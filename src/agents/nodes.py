"""主图节点实现（规格 §8）：resolve_l2 → plan → act → observe → r1_check → r2_reflect → respond。

节点不持有全局状态，全部通过 AgentState 传递；工具执行统一走 Toolkit。
消融开关（state["mode"]）：
- m0：无反思——生成即执行，执行失败直接如实报错（不重试不澄清）；
- m1：仅 R1 规则校验——hard 失败带原因重试（预算 2 次），soft 标记如实提示；
- m2：R1 + R2——hard/soft 都交 Reflector 独立复核，给出修正建议再重试，仍失败转 R3 澄清。
"""

from __future__ import annotations

import json
import logging

from src.agents import events as run_events
from src.agents.prompts import (CLARIFY_TEMPLATE, NARRATOR_SYSTEM, PLAN_SYSTEM,
                                SQL_SYSTEM, WRITE_SYSTEM)
from src.agents.reflector import r1_check as rule_check, r2_reflect
from src.agents.state import AgentState
from src.config import MAX_REFLECTION
from src.core.workbook import WorkbookManager, WriteOp
from src.llm import get_chat_model, invoke_json
from src.tools.toolkit import Toolkit, ToolError, result_to_context

logger = logging.getLogger(__name__)


def _toolkit(state: AgentState) -> Toolkit:
    return Toolkit(workbook_id=state["workbook_id"], session_id=state["session_id"])


def _schemas_brief(tk: Toolkit) -> str:
    parts = []
    for name in tk.sheet_names():
        parts.append(tk.inspect(name)["schema_prompt"])
    return "\n\n".join(parts) if parts else "（当前没有数据表）"


_STEP_STAGE = {
    "sql_query": "执行 SQL 查询",
    "chart": "生成图表",
    "diagnose": "诊断分析",
    "forecast": "趋势预测",
    "summarize": "汇总数据",
    "report": "整理报告素材",
    "write": "生成修改方案",
    "join": "关联数据表",
    "inspect": "读取表结构",
    "direct_answer": "整理回答",
}


def _new_trace(state: AgentState) -> list[dict]:
    return list(state.get("trace") or [])


def _kind_for(action: str) -> str:
    """失败步骤的结果 kind 与成功路径保持一致（r1_check 靠 kind 找 SQL 步骤）。"""
    return {"sql_query": "sql"}.get(action, action)


# ---------------------------------------------------------------------------
# 节点：resolve_l2 —— 工作记忆 + Mem0 检索（规格 §5）
# ---------------------------------------------------------------------------
def resolve_l2(state: AgentState) -> dict:
    run_events.check_cancel()
    run_events.emit("stage", "准备会话上下文")
    from src.memory.working import get_working_memory
    wm = get_working_memory()
    sid = state["session_id"]
    if state.get("workbook_id"):
        wm.set(sid, "active_workbook", state["workbook_id"])
        # 活动表默认取第一个（未设置过时）
        tk = _toolkit(state)
        try:
            names = tk.sheet_names()
            if names and wm.get(sid, "active_sheet") not in names:
                wm.set(sid, "active_sheet", names[0])
        except Exception:  # noqa: BLE001
            pass
    l2_context = wm.to_prompt(sid)

    from src.memory.longterm import LongTermMemory, get_longterm_memory
    if LongTermMemory._instance is None:
        run_events.emit("stage", "初始化记忆模型（首次约 30 秒）")
    else:
        run_events.emit("stage", "检索长期记忆")
    ltm = get_longterm_memory()
    l3 = ltm.search(state["user_input"], session_id=sid, limit=4)

    resolved = _resolve_reference(state, l2_context)
    return {"l2_context": l2_context, "l3_memories": l3, "resolved_input": resolved}


_REF_MARKERS = ("那", "它", "其", "这个", "那个", "刚才", "上面", "上述", "其中",
                "呢", "再", "换", "只看", "只留", "他", "她", "该")


def _is_referential(text: str) -> bool:
    t = (text or "").strip()
    if len(t) <= 10 and any(m in t for m in _REF_MARKERS):
        return True
    return any(m in t for m in ("刚才", "上面", "上述", "那", "其中", "呢"))


def _resolve_reference(state: AgentState, l2_context: str) -> str:
    """指代消解：把「那华南呢 / 它 / 刚才那个」改写为自包含问题（规格 §5 核心）。

    仅当检测到指代词且存在工作记忆/近期对话时触发一次轻量 LLM 改写；失败则用原文。
    """
    text = state["user_input"]
    has_history = bool(state.get("recent_turns")) or "上一条查询" in l2_context
    if not (has_history and _is_referential(text)):
        return text
    turns = "\n".join(
        f"{'用户' if t['role'] == 'user' else '助手'}: {t['content'][:150]}"
        for t in (state.get("recent_turns") or [])[-4:])
    prompt = (
        f"## 工作记忆\n{l2_context}\n\n"
        f"## 近期对话\n{turns or '（无）'}\n\n"
        f"## 用户这句话\n{text}\n\n"
        "请把这句话改写成一个**自包含的完整中文问题**：所有指代词（它/他/这个/刚才/那…呢）"
        "必须替换为对话中确定的对象（列名/表名/区域/负责人等），保留原意与范围。"
        "只输出改写后的问题本身，不要解释、不要引号。"
    )
    try:
        from langchain_core.messages import HumanMessage
        model = get_chat_model(temperature=0.0, max_tokens=200)
        resp = model.invoke([HumanMessage(content=prompt)])
        rewritten = (resp.content if isinstance(resp.content, str) else str(resp.content)).strip()
        if rewritten and 2 <= len(rewritten) <= 200 and not rewritten.startswith("{"):
            return rewritten
    except Exception as e:  # noqa: BLE001 —— 改写失败用原文，不阻断
        logger.warning("指代消解失败（用原文继续）：%s", e)
    return text


# ---------------------------------------------------------------------------
# 节点：plan —— 意图识别 + 步骤规划
# ---------------------------------------------------------------------------
def plan(state: AgentState) -> dict:
    run_events.check_cancel()
    run_events.emit("stage", "规划任务")
    tk = _toolkit(state)
    schemas = _schemas_brief(tk)
    resolved = state.get("resolved_input") or state["user_input"]
    prompt = (
        f"## 数据上下文\n{schemas}\n\n"
        f"## 工作记忆\n{state.get('l2_context') or '（空）'}\n\n"
        + (f"## 长期偏好（Mem0）\n" + "\n".join(f"- {m}" for m in state.get("l3_memories", []))
           if state.get("l3_memories") else "")
        + (f"\n## 近期对话\n" + "\n".join(
            f"{'用户' if t['role'] == 'user' else '助手'}: {t['content'][:120]}"
            for t in (state.get("recent_turns") or [])[-4:]) if state.get("recent_turns") else "")
        + f"\n\n## 用户问题（已消解指代的自包含版本）\n{resolved}\n"
        + (f"\n（用户原话：{state['user_input']}）" if resolved != state["user_input"] else "")
        + (f"\n## 上次尝试失败的原因（本次规划必须修正）\n{state.get('retry_hint')}"
           if state.get("retry_hint") else "")
        + "\n\n请输出 JSON 计划。"
    )
    try:
        model = get_chat_model(temperature=0.0)
        plan_dict = invoke_json(model, prompt, system=PLAN_SYSTEM)
    except Exception as e:  # noqa: BLE001
        logger.warning("规划失败，降级为 direct 回答：%s", e)
        return {"plan": {"intent": "chat", "reasoning": f"规划器不可用：{e}", "steps": []},
                "intent": "chat"}

    intent = plan_dict.get("intent") or "simple_query"
    if plan_dict.get("clarify"):
        intent = "clarify"
    return {"plan": plan_dict, "intent": intent}


# ---------------------------------------------------------------------------
# 节点：act —— 执行计划（简单路径直连工具；complex 走 supervisor）
# ---------------------------------------------------------------------------
def act(state: AgentState) -> dict:
    run_events.check_cancel()
    intent = state.get("intent") or "simple_query"
    run_events.emit("stage", "执行计划")
    steps = (state.get("plan") or {}).get("steps") or []
    artifacts = dict(state.get("artifacts") or {"charts": [], "res_ids": [], "report": ""})
    trace = _new_trace(state)
    hint = state.get("retry_hint") or ""

    # 复合请求 → supervisor 多 Agent 编排（规格 §7 铁律③：仅复合请求付多 Agent 开销）
    if intent == "complex" and not state.get("retry_hint"):
        from src.agents.supervisor_app import run_supervisor
        run_events.emit("stage", "多 Agent 编排")
        try:
            out = run_supervisor(state)
            return {"steps_results": list(state.get("steps_results") or []) + [
                        {"ok": True, "kind": "supervisor", "step_index": 0}],
                    "observation": out["observation"], "artifacts": {**artifacts, **out["artifacts"]},
                    "trace": trace + out["trace"],
                    "pending_write": out.get("pending_write"), "retry_hint": ""}
        except Exception as e:  # noqa: BLE001 —— supervisor 故障降级为顺序执行步骤
            logger.warning("supervisor 编排失败，降级为逐步执行：%s", e)

    results = list(state.get("steps_results") or [])
    pending_write = state.get("pending_write")
    observation_parts: list[str] = []

    # 报告类请求必须基于真实取数：计划里若没有查询步骤，自动前置两条简单查询
    #（规划器偶发省略 sql_query；拆分"总量 + 月度"两条单层聚合，避免巨型嵌套 SQL 被安全校验拒绝）
    steps = list(steps)
    if intent == "report" and not any(s.get("action") == "sql_query" for s in steps):
        scoped = state["user_input"]
        steps = [
            {"action": "sql_query",
             "question": (f"只按用户限定的范围筛选，做**单层**聚合，返回总额与记录数"
                          f"（不要嵌套子查询、不要 PIVOT）：{scoped}")},
            {"action": "sql_query",
             "question": (f"只按用户限定的范围筛选，按月份做**单层**分组汇总（不要嵌套子查询）：{scoped}")},
        ] + steps

    # 重试时只重跑失败的步骤
    failed_step = state.get("failed_step")
    step_range = [failed_step["index"]] if failed_step else range(len(steps))

    for i in step_range:
        run_events.check_cancel()
        if failed_step and i != failed_step.get("index"):
            continue
        step = steps[i] if i < len(steps) else {"action": intent}
        action = step.get("action", intent)
        run_events.emit("stage", _STEP_STAGE.get(action, f"执行 {action}"))
        try:
            out = _run_step(state, action, step, artifacts, trace, hint)
        except ToolError as e:
            out = {"ok": False, "action": action, "kind": _kind_for(action),
                   "question": step.get("question", ""), "error": str(e)}
        except Exception as e:  # noqa: BLE001 —— 工具故障不中断会话（AC-9）
            logger.exception("步骤执行异常")
            out = {"ok": False, "action": action, "kind": _kind_for(action),
                   "question": step.get("question", ""),
                   "error": f"{type(e).__name__}: {e}"}
        out["step_index"] = i
        out["action"] = action
        # 覆盖同一步的旧结果（重试场景）
        results = [r for r in results if r.get("step_index") != i] + [out]
        if out.get("ok"):
            observation_parts.append(_render_step(out))
        else:
            observation_parts.append(f"[步骤{i + 1} 失败] {out.get('error', '')}")
        if out.get("pending_write"):
            pending_write = out["pending_write"]

    observation = "\n\n".join(observation_parts)
    return {"steps_results": results, "observation": observation,
            "artifacts": artifacts, "trace": trace, "pending_write": pending_write,
            "retry_hint": ""}


def _run_step(state: AgentState, action: str, step: dict,
              artifacts: dict, trace: list, hint: str) -> dict:
    tk = _toolkit(state)

    if action == "inspect":
        info = tk.inspect(step.get("sheet"))
        trace.append({"tool": "inspect", "args": {"sheet": info["sheet"]}})
        return {"ok": True, "kind": "inspect", "text": info["schema_prompt"]}

    if action == "sql_query":
        question = step.get("question") or state["user_input"]
        # 兜底：若计划里的问题仍是纯指代（如“那华南呢”），用消解后的自包含问题
        resolved = state.get("resolved_input") or ""
        if _is_referential(question) and resolved and resolved != state["user_input"]:
            question = resolved
        step = {**step, "question": question}
        sql = _gen_sql(state, step, hint)
        res = tk.query(sql, purpose=question)
        trace.append({"tool": "sql_query", "args": {"sql": sql, "question": question},
                      "res_id": res["res_id"], "total_rows": res["total_rows"]})
        artifacts.setdefault("res_ids", []).append(res["res_id"])
        return {"ok": True, "kind": "sql", "result": res, "question": question}

    if action == "chart":
        spec = {"type": step.get("chart_type") or "bar",
                "title": step.get("title") or state["user_input"][:40]}
        res_ref = step.get("res_ref", "last")
        if res_ref == "last" or not step.get("res_id"):
            latest = tk.latest_result()
            if latest is None:
                raise ToolError("没有可用的查询结果可以画图，请先查询数据")
            spec["res_id"] = latest["res_id"]
        else:
            spec["res_id"] = step.get("res_id")
        chart = tk.chart(spec)
        trace.append({"tool": "chart", "args": {"type": spec["type"], "title": spec.get("title")},
                      "png": chart["png_path"]})
        artifacts.setdefault("charts", []).append(chart["png_path"])
        return {"ok": True, "kind": "chart", "chart": chart}

    if action == "diagnose":
        diag = tk.diagnose(step.get("sheet"))
        corr = tk.correlate(step.get("sheet"))
        trace.append({"tool": "diagnose+correlate", "args": {"sheet": diag["sheet"]}})
        return {"ok": True, "kind": "diagnose", "diagnose": diag, "correlation": corr}

    if action == "forecast":
        fc = tk.forecast(step.get("sheet"), step.get("date_col"), step.get("value_col"),
                         int(step.get("periods") or 3))
        trace.append({"tool": "forecast",
                      "args": {"date": fc["date_col"], "value": fc["value_col"]}})
        # 顺手把预测画成置信带图
        try:
            rows = [{"month": h["month"], "值": h["actual"]} for h in fc["history"]]
            rows += [{"month": f["month"], "值": f["forecast"],
                      "lo": f["lower"], "hi": f["upper"]} for f in fc["future"]]
            chart = tk.chart({"type": "line_band", "title": f"{fc['value_col']} 预测（含置信带）",
                              "columns": ["month", "值", "lo", "hi"],
                              "rows": [[r["month"], r["值"], r.get("lo"), r.get("hi")] for r in rows],
                              "x": "month", "y": ["值"], "y_lower": "lo", "y_upper": "hi"})
            artifacts.setdefault("charts", []).append(chart["png_path"])
        except ToolError:
            pass
        return {"ok": True, "kind": "forecast", "forecast": fc}

    if action == "summarize":
        mat = tk.summarize(step.get("sheet"))
        trace.append({"tool": "summarize", "args": {"sheet": mat["sheet"]}})
        return {"ok": True, "kind": "summarize", "materials": mat}

    if action == "report":
        mat = tk.summarize(step.get("sheet"))
        trace.append({"tool": "report_materials", "args": {"sheet": mat["sheet"]}})
        return {"ok": True, "kind": "report", "materials": mat}

    if action == "write":
        return _run_write_step(state, step, trace)

    if action == "join":
        pv = tk.join_preview(step.get("left"), step.get("right"), step.get("on", {}),
                             step.get("how", "left"))
        op = tk.join_apply(pv, step.get("new_sheet") or "关联结果")
        plan_result = tk.plan_write(op)[0]
        trace.append({"tool": "join_preview",
                      "args": {k: pv[k] for k in ("left", "right", "on", "how")},
                      "rows": pv["total_rows"]})
        return {"ok": True, "kind": "write",
                "diff_summary": plan_result.diff.description,
                "pending_write": _pending_write_payload(plan_result)}

    if action == "direct_answer":
        return {"ok": True, "kind": "chat", "text": step.get("answer", "")}

    raise ToolError(f"未支持的计划步骤：{action}")


def _gen_sql(state: AgentState, step: dict, hint: str) -> str:
    tk = _toolkit(state)
    prompt = (
        f"## 表结构\n{_schemas_brief(tk)}\n\n"
        f"## 要回答的问题\n{step.get('question') or state['user_input']}\n"
        + (f"\n## 修正提示（上一版 SQL 的问题）\n{hint}\n" if hint else "")
        + "\n请输出 SQL。"
    )
    model = get_chat_model(temperature=0.0)
    from langchain_core.messages import HumanMessage, SystemMessage
    resp = model.invoke([SystemMessage(content=SQL_SYSTEM), HumanMessage(content=prompt)])
    text = resp.content if isinstance(resp.content, str) else str(resp.content)
    import re
    m = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL)
    sql = (m.group(1) if m else text).strip().rstrip(";")
    if not sql:
        raise ToolError("模型没有生成 SQL")
    return sql


def _run_write_step(state: AgentState, step: dict, trace: list) -> dict:
    tk = _toolkit(state)
    request = step.get("request") or state["user_input"]
    prompt = (
        f"## 表结构\n{_schemas_brief(tk)}\n\n"
        f"## 工作记忆\n{state.get('l2_context') or ''}\n\n"
        f"## 用户的修改需求\n{request}\n\n"
        "请输出结构化写操作 JSON。"
    )
    model = get_chat_model(temperature=0.0)
    op_dict = invoke_json(model, prompt, system=WRITE_SYSTEM)
    if op_dict.get("op_type") in ("unsupported", None):
        raise ToolError(f"该需求暂不支持自动修改：{op_dict.get('reason', '无法映射为安全写操作')}")

    op_type = op_dict["op_type"]
    sheet = op_dict.get("sheet")
    params = dict(op_dict.get("params") or {})

    # 派生列：先在沙箱里算好表达式值
    if op_type == "add_column" and params.get("expression"):
        values = tk.eval_expression(sheet, params["expression"])
        params = {"name": params["name"], "values": values}

    op = WriteOp(op_type=op_type, sheet=sheet, params=params,
                 description=op_dict.get("description", ""))
    plan_result, diff = tk.plan_write(op)
    trace.append({"tool": f"write:{op_type}", "args": {"sheet": sheet, **params},
                  "n_cells": diff.n_cells})
    return {"ok": True, "kind": "write", "diff_summary": diff.description,
            "pending_write": _pending_write_payload(plan_result)}


def _pending_write_payload(plan_result) -> dict:
    from src.core.workbook import PlanResult
    assert isinstance(plan_result, PlanResult)
    diff = plan_result.diff
    return {
        "op": {"op_type": plan_result.op.op_type, "sheet": plan_result.op.sheet,
               "params": plan_result.op.params, "description": plan_result.op.description},
        "diff_html": diff.to_html(),
        "diff_dict": diff.to_dict(),
        "summary": diff.description,
        "n_cells": diff.n_cells,
        "needs_double": diff.needs_double_confirm(50),
    }


def _render_step(out: dict) -> str:
    kind = out.get("kind")
    if kind == "sql":
        return result_to_context(out["result"])
    if kind == "chart":
        c = out["chart"]
        rows = "\n".join(" | ".join(str(v) for v in r) for r in c["rows"][:10])
        return f"已生成{c['type']}图「{c['title']}」（数据 {len(c['rows'])} 行）：\n{rows}"
    if kind == "diagnose":
        diag = out["diagnose"]
        lines = [f"异常洞察（{diag['sheet']}）："]
        lines += [f"- [{f['type']}] {f['text']}" for f in diag["findings"]] or ["- 未发现明显异常"]
        corr = out.get("correlation") or {}
        if corr.get("results"):
            lines.append("相关性（Pearson，p<0.05 为显著）：")
            for r in corr["results"][:5]:
                sig = "显著" if r["significant"] else "不显著"
                lines.append(f"- {r['col_a']} × {r['col_b']}: r={r['pearson']}（p={r['pearson_p']:.3g}，{sig}，n={r['n']}）")
        return "\n".join(lines)
    if kind == "forecast":
        fc = out["forecast"]
        lines = [f"预测（{fc['method']}，R²={fc['r2']}）：{fc['note']}"]
        for f in fc["future"]:
            lines.append(f"- {f['month']}: 预测 {f['forecast']}（区间 {f['lower']} ~ {f['upper']}）")
        return "\n".join(lines)
    if kind in ("summarize", "report"):
        return json.dumps(out["materials"], ensure_ascii=False)[:4000]
    if kind == "inspect":
        return out["text"]
    if kind == "write":
        return f"已生成写操作预览：{out.get('diff_summary', '')}（等待用户确认）"
    return out.get("text", "")


# ---------------------------------------------------------------------------
# 节点：observe —— 汇总观察（complex 路径已在 act 里产出 observation）
# ---------------------------------------------------------------------------
def observe(state: AgentState) -> dict:
    # observation 已在 act 内组装；此节点负责把执行错误并入 error 字段供 respond 使用
    failed = [r for r in state.get("steps_results") or [] if not r.get("ok")]
    return {"error": failed[-1]["error"] if failed else ""}


# ---------------------------------------------------------------------------
# 节点：r1_check —— R1 规则校验 + 模式分支
# ---------------------------------------------------------------------------
def r1_check(state: AgentState) -> dict:
    run_events.check_cancel()
    run_events.emit("stage", "校验执行结果")
    mode = state.get("mode", "m2")
    if mode == "m0":
        # 无反思：直接进 respond（错误如实呈现，不重试）
        return {"r1_outcome": "pass"}

    results = state.get("steps_results") or []
    if state.get("pending_write") or state.get("intent") in ("clarify", "chat"):
        return {"r1_outcome": "pass"}

    # 找最近一个 sql 步骤校验；其他步骤（图表/诊断等）不适用 R1
    sql_steps = [r for r in results if r.get("kind") == "sql"]
    if not sql_steps:
        return {"r1_outcome": "pass"}
    last = sql_steps[-1]

    tk = _toolkit(state)
    full_rows = None
    try:
        sheet = tk.resolve_sheet(None)
        full_rows = tk.manager().get_df(state["workbook_id"], sheet).shape[0]
    except Exception:  # noqa: BLE001
        pass

    step_for_r1 = {"action": "sql_query", "question": last.get("question", "")}
    if last.get("ok") is False:
        # 失败步骤：把真实错误交给规则校验器（重试提示必须是具体原因）
        result_for_r1 = {"ok": False, "error": last.get("error", "执行失败")}
    else:
        result_for_r1 = last.get("result")
    verdict = rule_check(step_for_r1, result_for_r1, full_rows)

    if verdict.ok:
        return {"r1_outcome": "pass"}

    retries = int(state.get("retries") or 0)
    log = list(state.get("reflection_log") or [])
    failures = list(state.get("r1_failures") or []) + [
        {"step_index": last.get("step_index"), "level": verdict.level, "reason": verdict.reason}]

    def _hard_retry() -> dict:
        """确定性失败（报错/超时/列不存在）：错误信息本身即修正提示，直接重试。"""
        if retries >= MAX_REFLECTION:
            return {"r1_outcome": "clarify", "r1_failures": failures,
                    "reflection_log": log + [f"R1 第 {retries + 1} 次失败：{verdict.reason}（重试预算用尽，转澄清）"]}
        log.append(f"R1 第 {retries + 1} 次失败：{verdict.reason} → 带原因重试")
        return {"r1_outcome": "retry", "retries": retries + 1,
                "retry_hint": verdict.reason, "r1_failures": failures,
                "reflection_log": log, "failed_step": {"index": last.get("step_index")}}

    if mode == "m1":
        # 仅 R1：hard 失败带原因重试；soft 标记如实提示后通过
        if verdict.level == "hard":
            return _hard_retry()
        log.append(f"R1 提示：{verdict.reason}（已在回答中如实说明）")
        return {"r1_outcome": "pass", "r1_failures": failures, "reflection_log": log}

    # m2：确定性失败直接带原因重试；语义可疑（soft）才交 R2 独立复核
    if verdict.level == "hard":
        return _hard_retry()
    return {"r1_outcome": "reflect", "r1_failures": failures,
            "reflection_log": log + [f"R1 第 {retries + 1} 次未通过：{verdict.reason} → 交 R2 复核"],
            "failed_step": {"index": last.get("step_index")},
            "retry_hint": verdict.reason}


# ---------------------------------------------------------------------------
# 节点：r2_reflect —— R2 独立上下文复核（仅 m2）
# ---------------------------------------------------------------------------
def r2_reflect_node(state: AgentState) -> dict:
    run_events.check_cancel()
    run_events.emit("stage", "独立复核结果")
    results = state.get("steps_results") or []
    last = next((r for r in reversed(results) if r.get("kind") == "sql"), None)
    retries = int(state.get("retries") or 0)
    log = list(state.get("reflection_log") or [])

    if last is None:
        return {"r2_outcome": "clarify", "reflection_log": log + ["R2 无可复核的查询结果 → 转澄清"]}

    tk = _toolkit(state)
    schema_prompt = _schemas_brief(tk)
    res = last.get("result") or {}
    verdict = r2_reflect(state["user_input"], schema_prompt,
                         res.get("sql", ""), res.get("rows", []), res.get("columns", []),
                         execution_error=last.get("error", "") if not last.get("ok") else "")
    verdicts = list(state.get("r2_verdicts") or []) + [verdict]

    if verdict.get("answered"):
        note = verdict.get("reason", "")
        log.append(f"R2 复核：可回答（{note}）")
        observation = state.get("observation") or ""
        if note and "无数据" in note:
            observation += f"\n\n[复核提示] {note}"
        return {"r2_outcome": "pass", "r2_verdicts": verdicts,
                "reflection_log": log, "observation": observation}
    if verdict.get("need_clarify") or retries >= MAX_REFLECTION:
        log.append(f"R2 复核：无法回答（{verdict.get('reason')}）→ 转澄清")
        question = verdict.get("clarify_question") or verdict.get("fix_suggestion") or "请补充说明你的需求"
        return {"r2_outcome": "clarify", "r2_verdicts": verdicts,
                "reflection_log": log, "retry_hint": question}
    log.append(f"R2 复核：需要修正（{verdict.get('fix_suggestion')}）→ 重试")
    return {"r2_outcome": "retry", "r2_verdicts": verdicts, "retries": retries + 1,
            "retry_hint": verdict.get("fix_suggestion") or verdict.get("reason", ""),
            "reflection_log": log}


# ---------------------------------------------------------------------------
# 节点：respond —— 叙述者成文 / 澄清 / 确认卡
# ---------------------------------------------------------------------------
def respond(state: AgentState) -> dict:
    run_events.check_cancel()
    run_events.emit("stage", "生成回答")
    sid = state["session_id"]

    # 1) 写操作待确认：确定性文案，不走 LLM
    if state.get("pending_write"):
        pw = state["pending_write"]
        double = "影响超过 50 个单元格，确认后还需**再次确认**才会执行。" if pw.get("needs_double") else ""
        response = (
            f"我准备好了以下修改，请确认后执行：\n\n**{pw['summary']}**\n\n{double}"
            f"点击「确认执行」落盘（可撤销），或「取消」。"
        )
        return {"response": response, "confirm_required": True, "need_clarification": False}

    # 2) 澄清（R3）
    if state.get("intent") == "clarify" or state.get("r2_outcome") == "clarify" \
            or state.get("r1_outcome") == "clarify":
        clarify = (state.get("plan") or {}).get("clarify") or {}
        question = clarify.get("question") or state.get("retry_hint") or "请补充说明你的需求"
        hint = ""
        tk = _toolkit(state)
        try:
            cols = tk.columns_map()
            if cols:
                hint = "当前可用表与列：" + "；".join(
                    f"「{s}」({'、'.join(cs[:12])})" for s, cs in cols.items())
        except Exception:  # noqa: BLE001
            pass
        return {"response": CLARIFY_TEMPLATE.format(question=question, hint=hint),
                "need_clarification": True, "confirm_required": False}

    # 3) 执行全部失败且无反思路径兜底（m0）：如实报错
    results = state.get("steps_results") or []
    if results and all(not r.get("ok") for r in results):
        errs = "；".join(r.get("error", "") for r in results)
        return {"response": f"这一步我没有做到：{errs}\n\n你可以换个说法，或检查数据后再试。",
                "need_clarification": True, "confirm_required": False}

    # 4) 寒暄/能力询问：轻量直答
    if state.get("intent") == "chat" and not state.get("observation"):
        try:
            model = get_chat_model(temperature=0.3)
            from langchain_core.messages import HumanMessage, SystemMessage
            resp = model.invoke([
                SystemMessage(content="你是表格数据分析助手「表格管家」。简短友好地回答用户；"
                                      "介绍能力时提到：分析查询、出图、清洗修改（带确认可撤销）、预测诊断、"
                                      "生成报告、导出 xlsx/PDF。"),
                HumanMessage(content=state["user_input"]),
            ])
            text = resp.content if isinstance(resp.content, str) else str(resp.content)
        except Exception as e:  # noqa: BLE001
            text = f"（对话服务暂不可用：{e}）"
        return {"response": text, "need_clarification": False, "confirm_required": False}

    # 5) 叙述者成文（基于观察，绝不编造）
    artifacts = state.get("artifacts") or {}
    charts_note = "\n".join(
        f"- 第 {i + 1} 张图表已显示在右侧「图表」面板"
        for i, _ in enumerate(artifacts.get("charts", [])))
    if charts_note:
        charts_note += "\n（回答中不要输出图片文件名或本地路径。）"
    summary_like = (state.get("intent") in ("report", "complex")
                    or any(k in state["user_input"] for k in ("小结", "总结", "报告")))
    length_hint = ("\n\n【篇幅要求】这是总结/报告类请求：正文严格 ≤300 字；用户未要求表格就不要表格；"
                   "不要输出查询溯源行；按「整体概况 → 关键发现 → 建议」三段组织，"
                   "不要展开额外分析或多给小标题。"
                   if summary_like else "")
    prompt = (
        f"## 用户问题\n{state['user_input']}\n\n"
        f"## 执行观察（唯一数据来源，禁止编造）\n{state.get('observation') or '（无观察内容）'}\n"
        + (f"\n## 已生成图表\n{charts_note}\n" if charts_note else "")
        + (f"\n## 近期对话\n" + "\n".join(
            f"{'用户' if t['role'] == 'user' else '助手'}: {t['content'][:100]}"
            for t in (state.get("recent_turns") or [])[-4:]) if state.get("recent_turns") else "")
        + (f"\n\n## R2 复核提示\n{state.get('retry_hint')}" if state.get("retry_hint") else "")
        + length_hint
        + "\n\n请生成最终回答。"
    )
    try:
        # 报告类限制输出长度，防止正文啰嗦超出 300 字要求
        if summary_like:
            model = get_chat_model(temperature=0.2, max_tokens=1024)
        else:
            model = get_chat_model(temperature=0.3)
        from langchain_core.messages import HumanMessage, SystemMessage
        messages = [SystemMessage(content=NARRATOR_SYSTEM), HumanMessage(content=prompt)]
        ev = run_events.current_events()
        if ev is not None:
            parts: list[str] = []
            try:
                for chunk in model.stream(messages):
                    piece = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
                    if piece:
                        parts.append(piece)
                        run_events.emit("token", piece)
                text = "".join(parts)
            except Exception:  # noqa: BLE001 —— 流式中断则退回一次性调用
                resp = model.invoke(messages)
                text = resp.content if isinstance(resp.content, str) else str(resp.content)
        else:
            resp = model.invoke(messages)
            text = resp.content if isinstance(resp.content, str) else str(resp.content)
    except Exception as e:  # noqa: BLE001 —— LLM 故障也要把数据带给用户（AC-9）
        text = f"（叙述服务暂时不可用：{e}）以下为原始查询结果：\n\n{state.get('observation', '')}"

    return {"response": text, "need_clarification": False, "confirm_required": False}
