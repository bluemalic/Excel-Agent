"""LangGraph 主状态图（规格 §8）：

    resolve_l2 → plan → act → observe → r1_check → (r2_reflect) → respond
    边：r1/r2 失败 → act 重试（预算 MAX_REFLECTION=2）→ 仍失败 → clarify（R3）
    写操作：act 生成 diff → respond 输出确认卡 → 用户确认后由服务层 apply（人在回路）
"""

from __future__ import annotations

import logging
import threading

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from src.agents import events as run_events
from src.agents import nodes
from src.agents.state import AgentState, initial_state

logger = logging.getLogger(__name__)

_graph = None
_graph_lock = threading.Lock()
_checkpointer: BaseCheckpointSaver | None = None


def _route_after_r1(state: AgentState) -> str:
    outcome = state.get("r1_outcome")
    if outcome == "retry":
        return "act"
    if outcome == "reflect":
        return "r2_reflect"
    if outcome == "clarify":
        return "respond"
    return "respond"


def _route_after_r2(state: AgentState) -> str:
    outcome = state.get("r2_outcome")
    if outcome == "retry":
        return "act"
    return "respond"


def build_graph(checkpointer: BaseCheckpointSaver | None = None):
    """构建并缓存主编排图。默认用 SQLite checkpoint（L1 会话态持久化，规格 §5）。"""
    global _graph, _checkpointer
    with _graph_lock:
        if _graph is not None and (checkpointer is None or _checkpointer is checkpointer):
            return _graph
        if checkpointer is not None:
            _checkpointer = checkpointer
        elif _checkpointer is None:
            _checkpointer = _make_sqlite_checkpointer()
        g = StateGraph(AgentState)
        g.add_node("resolve_l2", nodes.resolve_l2)
        g.add_node("plan", nodes.plan)
        g.add_node("act", nodes.act)
        g.add_node("observe", nodes.observe)
        g.add_node("r1_check", nodes.r1_check)
        g.add_node("r2_reflect", nodes.r2_reflect_node)
        g.add_node("respond", nodes.respond)

        g.set_entry_point("resolve_l2")
        g.add_edge("resolve_l2", "plan")
        g.add_edge("plan", "act")
        g.add_edge("act", "observe")
        g.add_edge("observe", "r1_check")
        # 重试回路：r1_check/r2_reflect 的条件边可回到 act，act 后固定走 observe → r1_check 再校验
        g.add_conditional_edges("r1_check", _route_after_r1,
                                {"act": "act", "r2_reflect": "r2_reflect", "respond": "respond"})
        g.add_conditional_edges("r2_reflect", _route_after_r2,
                                {"act": "act", "respond": "respond"})
        g.add_edge("respond", END)

        _checkpointer = checkpointer or MemorySaver()
        _graph = g.compile(checkpointer=_checkpointer)
        return _graph


def _make_sqlite_checkpointer() -> BaseCheckpointSaver:
    """SQLite 持久 checkpoint；失败（如目录不可写）降级内存 saver。"""
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver

        from src import config
        config.ensure_dirs()
        conn = __import__("sqlite3", fromlist=["connect"]).connect(
            str(config.DATA_ROOT / "checkpoints.sqlite"), check_same_thread=False)
        return SqliteSaver(conn)
    except Exception as e:  # noqa: BLE001
        logging.warning("SqliteSaver 不可用，降级 MemorySaver：%s", e)
        return MemorySaver()


def run_turn(session_id: str, workbook_id: str | None, user_input: str,
             mode: str = "m2", chat_summary: str = "", recent_turns: list | None = None,
             events=None):
    """执行一轮对话，返回最终 AgentState（events 非空时实时推送阶段/逐字输出）。"""
    graph = build_graph()
    state = initial_state(session_id, workbook_id, user_input, mode=mode)
    state["chat_summary"] = chat_summary
    state["recent_turns"] = recent_turns or []
    config = {"configurable": {"thread_id": session_id}, "recursion_limit": 64}
    token = run_events.bind(events)
    try:
        final = graph.invoke(state, config=config)
    except run_events.TurnCancelled:
        final = dict(state)
        final.update({
            "response": "已停止当前任务。你可以调整问题后重新发送。",
            "need_clarification": False, "confirm_required": False,
            "pending_write": None,
            "reflection_log": list(state.get("reflection_log") or []) + ["用户停止了执行"],
        })
    finally:
        run_events.unbind(token)
    return final
