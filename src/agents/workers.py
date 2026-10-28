"""Supervisor 下的专职 Worker（规格 §7）：工具集 + 角色装配。

- 只有 writer 挂写工具（铁律①：写操作单一入口；串行由 WorkbookManager 全局锁保证）；
- data_query / analyst 均为只读工具，可被并行调度（铁律②）；
- 简单请求不经过本模块（铁律③），主图直连工具。
"""

from __future__ import annotations

import json
import logging

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

from src.agents.prompts import SQL_SYSTEM, WRITE_SYSTEM
from src.core.workbook import WriteOp
from src.llm import get_chat_model, invoke_json
from src.tools.toolkit import Toolkit, ToolError, result_to_context

logger = logging.getLogger(__name__)


def build_tools(ctx: dict) -> list:
    """ctx: 可变共享上下文 {workbook_id, session_id, trace, charts, res_ids, pending_write}。"""
    tk_holder: dict = {}

    def _tk() -> Toolkit:
        if tk_holder.get("tk") is None:
            tk_holder["tk"] = Toolkit(workbook_id=ctx["workbook_id"], session_id=ctx["session_id"])
        return tk_holder["tk"]

    def _trace(entry: dict) -> None:
        ctx.setdefault("trace", []).append(entry)

    @tool
    def sql_query(question: str) -> str:
        """生成并执行一条只读 SQL 查询来回答数据问题。输入：要回答的问题（独立完整、含过滤/聚合要求）。"""
        t = _tk()
        schemas = "\n\n".join(t.inspect(n)["schema_prompt"] for n in t.sheet_names())
        model = get_chat_model(temperature=0.0)
        from langchain_core.messages import HumanMessage, SystemMessage
        resp = model.invoke([SystemMessage(content=SQL_SYSTEM),
                             HumanMessage(content=f"## 表结构\n{schemas}\n\n## 问题\n{question}\n请输出 SQL。")])
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        import re
        m = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL)
        sql = (m.group(1) if m else text).strip().rstrip(";")
        res = t.query(sql, purpose=question)
        _trace({"tool": "sql_query", "args": {"sql": sql}, "res_id": res["res_id"]})
        ctx.setdefault("res_ids", []).append(res["res_id"])
        return result_to_context(res) + f"\n[res_id={res['res_id']}]"

    @tool
    def diagnose(sheet: str = "") -> str:
        """对工作表做异常洞察（空值/离群/环比骤降）与相关性分析（含 p 值）。输入：sheet 名（可空=当前表）。"""
        t = _tk()
        diag = t.diagnose(sheet or None)
        corr = t.correlate(sheet or None)
        _trace({"tool": "diagnose", "args": {"sheet": diag["sheet"]}})
        lines = [f"异常洞察（{diag['sheet']}）："]
        lines += [f"- [{f['type']}] {f['text']}" for f in diag["findings"]] or ["- 未发现明显异常"]
        if corr.get("results"):
            lines.append("相关性：")
            for r in corr["results"][:6]:
                sig = "显著" if r["significant"] else "不显著"
                lines.append(f"- {r['col_a']} × {r['col_b']}: r={r['pearson']}（p={r['pearson_p']:.3g}，{sig}）")
        return "\n".join(lines)

    @tool
    def forecast(sheet: str = "", date_col: str = "", value_col: str = "", periods: int = 3) -> str:
        """轻量趋势预测（线性回归+置信带）。输入：sheet 名、日期列、数值列、预测期数（月）。"""
        t = _tk()
        fc = t.forecast(sheet or None, date_col or None, value_col or None, int(periods))
        _trace({"tool": "forecast", "args": {"date": fc["date_col"], "value": fc["value_col"]}})
        lines = [f"预测（{fc['method']}，R²={fc['r2']}）——{fc['note']}"]
        for f in fc["future"]:
            lines.append(f"- {f['month']}: {f['forecast']}（{f['lower']} ~ {f['upper']}）")
        return "\n".join(lines)

    @tool
    def make_chart(res_id: str = "", chart_type: str = "bar", title: str = "") -> str:
        """把查询结果画成图。输入：res_id（sql_query 返回的编号，空=最近一次结果）、图型 bar/line/pie、标题。"""
        t = _tk()
        spec = {"type": chart_type or "bar", "title": title or "图表"}
        if res_id:
            spec["res_id"] = res_id
        else:
            latest = t.latest_result()
            if latest is None:
                raise ToolError("没有可画图的查询结果，请先调用 sql_query")
            spec["res_id"] = latest["res_id"]
        chart = t.chart(spec)
        _trace({"tool": "chart", "args": {"type": chart_type, "title": title}, "png": chart["png_path"]})
        ctx.setdefault("charts", []).append(chart["png_path"])
        return f"图表已生成：{chart['png_path']}（数据 {len(chart['rows'])} 行）"

    @tool
    def summarize(sheet: str = "") -> str:
        """汇总工作表关键统计（描述+诊断+趋势素材）。输入：sheet 名（可空=当前表）。"""
        t = _tk()
        mat = t.summarize(sheet or None)
        _trace({"tool": "summarize", "args": {"sheet": mat["sheet"]}})
        return json.dumps(mat, ensure_ascii=False)[:3500]

    @tool
    def propose_write(op_type: str, sheet: str, params_json: str, description: str = "") -> str:
        """提出一个结构化写操作（唯一能改表的工具）。输入：操作类型、sheet 名、参数 JSON、中文描述。
        可用 op_type：set_cell/set_range/add_rows/delete_rows/fill_missing/drop_duplicates/
        cast_column/add_column(参数含 expression)/rename_column/drop_column/style_cells。
        参数与列名必须来自 schema；执行前会生成 diff 供用户确认。"""
        t = _tk()
        params = json.loads(params_json)
        # 派生列：沙箱预计算表达式
        if op_type == "add_column" and params.get("expression"):
            values = t.eval_expression(sheet, params["expression"])
            params = {"name": params["name"], "values": values}
        op = WriteOp(op_type=op_type, sheet=sheet, params=params, description=description)
        plan_result, diff = t.plan_write(op)
        ctx["pending_write"] = {
            "op": {"op_type": op_type, "sheet": sheet, "params": params, "description": description},
            "diff_html": diff.to_html(), "diff_dict": diff.to_dict(),
            "summary": diff.description, "n_cells": diff.n_cells,
            "needs_double": diff.needs_double_confirm(50),
        }
        _trace({"tool": f"write:{op_type}", "args": {"sheet": sheet, **params}, "n_cells": diff.n_cells})
        return (f"写操作已生成待确认：{diff.description}（影响 {diff.n_cells} 个单元格）。"
                f"这是唯一入口，用户确认后才会执行。")

    @tool
    def get_schema(sheet: str = "") -> str:
        """查看工作表结构（列名/类型/样本值/缺失率）。输入：sheet 名（可空=当前表）。"""
        t = _tk()
        info = t.inspect(sheet or None)
        _trace({"tool": "inspect", "args": {"sheet": info["sheet"]}})
        return info["schema_prompt"] + f"\n\n全部工作表：{info['sheets']}"

    return [get_schema, sql_query, diagnose, forecast, make_chart, summarize, propose_write]


_WORKER_SPECS = [
    ("planner", "复合请求拆解：把用户需求拆成有序子任务（只拆解不执行，输出简短清单）", []),
    ("data_query", "读数据专家：生成 SQL 查询/聚合/统计，返回表格数据", ["get_schema", "sql_query"]),
    ("analyst", "深度分析：异常洞察、相关性+显著性、趋势预测", ["get_schema", "diagnose", "forecast", "sql_query"]),
    ("visualizer", "可视化：把查询/分析结果画成合适的图表", ["make_chart", "get_schema"]),
    ("writer", "唯一可修改表格的 Agent：结构化写操作，生成 diff 待确认", ["get_schema", "propose_write"]),
    ("narrator", "叙述者：把全部结果整理成中文结论/报告（100-300 字，数字必须来自结果，不使用 emoji）", ["summarize"]),
]


def build_workers(ctx: dict, model=None) -> list:
    """按角色表创建 react agents（名字即 supervisor 路由目标）。"""
    model = model or get_chat_model(temperature=0.0)
    tools = {t.name: t for t in build_tools(ctx)}
    workers = []
    for name, prompt, tool_names in _WORKER_SPECS:
        w_tools = [tools[n] for n in dict.fromkeys(tool_names) if n in tools]
        workers.append(create_react_agent(model, w_tools, name=name, prompt=prompt))
    return workers
