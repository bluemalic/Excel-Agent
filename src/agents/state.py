"""LangGraph 主图状态定义（规格 §8）。

说明：所有字段用普通（last-write-wins）语义。单次 invoke 内部由节点完整重写
列表字段；配合 SqliteSaver checkpointer（thread_id=会话）时，每轮 invoke 以
initial_state 重置全部字段，避免上一轮执行痕迹泄漏。
"""

from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    # 输入
    session_id: str
    workbook_id: str | None
    user_input: str
    mode: str                       # 反思消融开关：m0 / m1 / m2（默认 m2）

    # L1：压缩后的对话历史摘要 + 最近几轮原文（记忆层）
    chat_summary: str
    recent_turns: list[dict]        # [{role, content}...]

    # L2 解析产物
    resolved_input: str             # 指代消解后的自包含问题（无指代时=user_input）
    l2_context: str
    l3_memories: list[str]

    # 规划产物
    plan: dict                      # {intent, reasoning, clarify, steps}
    intent: str

    # 执行
    steps_results: list[dict]       # 每步执行结果（单轮内完整重写）
    observation: str                # 给叙述者的观察文本
    artifacts: dict                 # {charts: [png路径], res_ids: [...], report: str}
    pending_write: dict | None      # {"op": {...}, "diff_html": ..., "summary": ...}

    # 反思（R1/R2/R3）
    r1_failures: list[dict]         # {step_index, level, reason}
    r2_verdicts: list[dict]
    retries: int                    # 当前重试次数（预算 MAX_REFLECTION=2）
    reflection_log: list[str]       # UI 反思轨迹（人话）
    retry_hint: str                 # 回喂给 act 的修正提示

    # 输出
    response: str
    need_clarification: bool
    confirm_required: bool          # True=有写操作等用户确认
    trace: list[dict]               # 工具调用记录（「查看执行的查询」）
    error: str


def initial_state(session_id: str, workbook_id: str | None, user_input: str,
                  mode: str = "m2") -> dict:
    return {
        "session_id": session_id,
        "workbook_id": workbook_id,
        "user_input": user_input,
        "mode": mode,
        "chat_summary": "",
        "recent_turns": [],
        "resolved_input": user_input,
        "l2_context": "",
        "l3_memories": [],
        "plan": {},
        "intent": "",
        "steps_results": [],
        "observation": "",
        "artifacts": {"charts": [], "res_ids": [], "report": ""},
        "pending_write": None,
        "r1_failures": [],
        "r2_verdicts": [],
        "retries": 0,
        "reflection_log": [],
        "retry_hint": "",
        "response": "",
        "need_clarification": False,
        "confirm_required": False,
        "trace": [],
        "error": "",
    }
