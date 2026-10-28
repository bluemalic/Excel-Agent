"""langgraph-supervisor 编排入口（规格 §7）。

Supervisor 持有 workbook 唯一句柄；写操作只有 writer 的 propose_write 工具能提出
（生成 diff 待确认，不直接落盘）；读/分析工具只读。app 按 (session, workbook) 缓存，
ctx 每次调用前清空。
"""

from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage, SystemMessage

from src.agents.prompts import SUPERVISOR_PROMPT
from src.agents.state import AgentState
from src.llm import get_chat_model
from src.tools.toolkit import Toolkit

logger = logging.getLogger(__name__)

_cache: dict[tuple, tuple] = {}


def _get_app(session_id: str, workbook_id: str):
    from langgraph_supervisor import create_supervisor

    from src.agents.workers import build_workers

    key = (session_id, workbook_id)
    if key not in _cache:
        ctx: dict = {"workbook_id": workbook_id, "session_id": session_id,
                     "trace": [], "charts": [], "res_ids": [], "pending_write": None}
        model = get_chat_model(temperature=0.0)
        workers = build_workers(ctx, model)
        app = create_supervisor(
            model=model,
            agents=workers,
            supervisor_name="supervisor",
            prompt=SUPERVISOR_PROMPT,
            output_mode="last_message",
            add_handoff_back_messages=True,
            max_history=6,
        ).compile()
        _cache[key] = (app, ctx)
        if len(_cache) > 4:
            _cache.pop(next(iter(_cache)))
    return _cache[key]


def run_supervisor(state: AgentState) -> dict:
    """复合请求走多 Agent 编排，返回 {observation, artifacts, trace, pending_write}。"""
    session_id, workbook_id = state["session_id"], state["workbook_id"]
    app, ctx = _get_app(session_id, workbook_id)
    # 每次调用重置共享上下文（缓存 app 复用）
    ctx.update({"trace": [], "charts": [], "res_ids": [], "pending_write": None})

    tk = Toolkit(workbook_id=workbook_id, session_id=session_id)
    schemas = "\n\n".join(tk.inspect(n)["schema_prompt"] for n in tk.sheet_names()) or "（无数据表）"

    messages = [
        SystemMessage(content=f"当前工作簿的表结构：\n{schemas}\n\n工作记忆：{state.get('l2_context', '')}"),
        HumanMessage(content=state["user_input"]),
    ]
    result = app.invoke({"messages": messages}, config={"recursion_limit": 48})
    final = result["messages"][-1]
    final_text = final.content if isinstance(final.content, str) else str(final.content)

    observation = f"[多 Agent 编排结果]\n{final_text}"
    if ctx.get("charts"):
        observation += "\n已生成图表：" + "、".join(ctx["charts"])
    return {
        "observation": observation,
        "artifacts": {"charts": list(ctx.get("charts") or []),
                      "res_ids": list(ctx.get("res_ids") or []), "report": ""},
        "trace": list(ctx.get("trace") or []),
        "pending_write": ctx.get("pending_write"),
    }
