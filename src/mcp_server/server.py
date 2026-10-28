"""FastMCP Server：把工具封装为标准 MCP Server（规格 §12）。

Tools:
  excel_schema        查看 workbook 的表结构
  excel_query         执行只读 SQL（同沙箱校验）
  excel_chart         出图
  excel_join          多表关联预览
  excel_propose_write 提出结构化写操作（返回 diff，不落盘）
  excel_confirm_write 确认执行（>50 单元格需 double_confirm=true）
  excel_cancel_write  取消待定写操作
  excel_undo          撤销上一步
Resources:
  excel://{workbook_id}/schema   只读元数据（schema 摘要，绝不塞整表）

启动：.venv/Scripts/python.exe -m src.mcp_server   （stdio 传输）
MCP 侧使用独立会话作用域 "mcp"；数据根目录与主应用一致（EXCEL_AGENT_DATA_ROOT）。
"""

from __future__ import annotations

import json

from fastmcp import FastMCP

from src.core.workbook import WorkbookManager, WriteOp
from src.service import AgentService
from src.tools.toolkit import Toolkit, ToolError

mcp = FastMCP("excel-agent", instructions="Excel 对话式数据分析智能体的 MCP Server："
                                          "查询/分析/出图走只读工具；修改走 propose→confirm 两步（可撤销）。")

_MCP_SESSION = "mcp"


def _service() -> AgentService:
    return AgentService(_MCP_SESSION)


def _tk(workbook_id: str) -> Toolkit:
    return Toolkit(workbook_id=workbook_id, session_id=_MCP_SESSION)


def _resolve_workbook(workbook_id_or_name: str) -> str:
    svc = _service()
    if svc.workbook_id == workbook_id_or_name:
        return workbook_id_or_name
    for row in svc.storage.list_workbooks():
        if row["workbook_id"] == workbook_id_or_name or row["name"] == workbook_id_or_name:
            return row["workbook_id"]
    raise ToolError(f"找不到工作簿「{workbook_id_or_name}」。可用："
                    f"{[(r['workbook_id'], r['name']) for r in svc.storage.list_workbooks()]}")


@mcp.tool
def list_workbooks() -> str:
    """列出所有已导入的工作簿（id 与名称）。"""
    rows = _service().storage.list_workbooks()
    return json.dumps([{"workbook_id": r["workbook_id"], "name": r["name"],
                        "origin": r["origin"]} for r in rows], ensure_ascii=False)


@mcp.tool
def excel_schema(workbook_id: str, sheet: str = "") -> str:
    """查看工作簿的表结构（列名/类型/样本值/缺失率），绝不返回整表数据。"""
    tk = _tk(_resolve_workbook(workbook_id))
    info = tk.inspect(sheet or None)
    return json.dumps(info, ensure_ascii=False)


@mcp.tool
def excel_query(workbook_id: str, sql: str) -> str:
    """在沙箱中执行一条只读 SELECT（DuckDB 语法，中文列名用双引号），返回结果与 res_id。"""
    tk = _tk(_resolve_workbook(workbook_id))
    res = tk.query(sql)
    return json.dumps(res, ensure_ascii=False)


@mcp.tool
def excel_chart(workbook_id: str, res_id: str = "", chart_type: str = "bar",
                title: str = "") -> str:
    """把查询结果（res_id，空=最近一次）画成 bar/line/pie 图，返回 PNG 路径与数据。"""
    tk = _tk(_resolve_workbook(workbook_id))
    spec: dict = {"type": chart_type or "bar", "title": title or "图表"}
    if res_id:
        spec["res_id"] = res_id
    else:
        latest = tk.latest_result()
        if latest is None:
            raise ToolError("没有可画图的查询结果，请先 excel_query")
        spec["res_id"] = latest["res_id"]
    return json.dumps(tk.chart(spec), ensure_ascii=False)


@mcp.tool
def excel_join(workbook_id: str, left: str, right: str, on: str, how: str = "left") -> str:
    """多表关联预览（只读不落盘）。返回合并行数与前 50 行样例；确认后用 excel_propose_write 落表。"""
    tk = _tk(_resolve_workbook(workbook_id))
    return json.dumps(tk.join_preview(left, right, on, how), ensure_ascii=False)


@mcp.tool
def excel_propose_write(workbook_id: str, op_type: str, sheet: str,
                        params_json: str, description: str = "") -> str:
    """提出结构化写操作（set_cell/fill_missing/drop_duplicates/cast_column/add_column/
    rename_column/drop_column/style_cells/add_rows/delete_rows/set_range），返回 diff 预览，
    不落盘。确认请调用 excel_confirm_write。"""
    tk = _tk(_resolve_workbook(workbook_id))
    params = json.loads(params_json)
    if op_type == "add_column" and params.get("expression"):
        values = tk.eval_expression(sheet, params["expression"])
        params = {"name": params["name"], "values": values}
    plan, diff = tk.plan_write(WriteOp(op_type=op_type, sheet=sheet, params=params,
                                       description=description))
    svc = _service()
    svc.wm.set(_MCP_SESSION, "pending_write", {
        "op": {"op_type": op_type, "sheet": sheet, "params": params,
               "description": description},
        "summary": diff.description, "n_cells": diff.n_cells,
        "needs_double": diff.needs_double_confirm(50), "stage": 1,
    })
    return json.dumps({"ok": True, "summary": diff.description, "n_cells": diff.n_cells,
                       "changes": [c.to_dict() for c in diff.changes[:20]],
                       "needs_double": diff.needs_double_confirm(50)}, ensure_ascii=False)


@mcp.tool
def excel_confirm_write(workbook_id: str, double_confirm: bool = False) -> str:
    """确认执行最近一次 excel_propose_write 提出的修改。影响>50 单元格时需 double_confirm=true。"""
    svc = _service()
    svc.set_workbook(_resolve_workbook(workbook_id))
    pending = svc.wm.get(_MCP_SESSION, "pending_write")
    if not pending:
        raise ToolError("没有待确认的写操作")
    if pending.get("needs_double") and not double_confirm:
        pending["stage"] = 2
        svc.wm.set(_MCP_SESSION, "pending_write", pending)
        return json.dumps({"ok": False, "reason": f"影响 {pending['n_cells']} 个单元格（>50），"
                                                  f"请带 double_confirm=true 再次确认"},
                          ensure_ascii=False)
    manager = WorkbookManager.instance()
    op = pending["op"]
    plan = manager.plan_write(svc.workbook_id, WriteOp(
        op_type=op["op_type"], sheet=op["sheet"], params=op.get("params") or {},
        description=op.get("description", "")))
    applied = manager.apply_write(svc.workbook_id, plan)
    svc.wm.set(_MCP_SESSION, "pending_write", None)
    return json.dumps({"ok": True, **applied}, ensure_ascii=False)


@mcp.tool
def excel_cancel_write(workbook_id: str) -> str:
    """取消待定的写操作。"""
    _service().wm.set(_MCP_SESSION, "pending_write", None)
    return json.dumps({"ok": True, "message": "已取消"}, ensure_ascii=False)


@mcp.tool
def excel_undo(workbook_id: str) -> str:
    """撤销最近一次写操作（逆操作日志 + 版本快照）。"""
    svc = _service()
    svc.set_workbook(_resolve_workbook(workbook_id))
    return json.dumps({"ok": True, "message": svc.undo()}, ensure_ascii=False)


@mcp.resource("excel://{workbook_id}/schema")
def workbook_schema(workbook_id: str) -> str:
    """Resource：workbook 的 schema 摘要（客户端只读元数据，不塞整表）。"""
    tk = _tk(_resolve_workbook(workbook_id))
    parts = [tk.inspect(name)["schema_prompt"] for name in tk.sheet_names()]
    return "\n\n".join(parts)


if __name__ == "__main__":
    mcp.run()
