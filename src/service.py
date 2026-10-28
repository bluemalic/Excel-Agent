"""AgentService：面向 UI / 评测 / MCP 的统一门面。

- 一轮对话 = run_turn（主图）+ 记忆回写 + 待确认写操作登记；
- 写操作确认流：chat 产生 pending_write → 用户确认（>50 单元格需二次）→ apply_write；
- L1 会话压缩：历史超阈值时用 LLM 把旧轮摘要滚动合并（规格 §5 L1）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.agents.graph import run_turn
from src.config import L1_COMPRESS_THRESHOLD
from src.core.workbook import WorkbookManager, WriteOp
from src.llm import get_chat_model
from src.memory.working import get_working_memory
from src.tools.toolkit import Toolkit

logger = logging.getLogger(__name__)


@dataclass
class TurnResult:
    response: str
    need_clarification: bool = False
    confirm_required: bool = False
    artifacts: dict = field(default_factory=dict)
    trace: list = field(default_factory=list)
    reflection_log: list = field(default_factory=list)
    pending_write: dict | None = None
    intent: str = ""
    plan: dict = field(default_factory=dict)
    retries: int = 0

    @classmethod
    def from_state(cls, state: dict) -> "TurnResult":
        return cls(
            response=state.get("response", ""),
            need_clarification=bool(state.get("need_clarification")),
            confirm_required=bool(state.get("confirm_required")),
            artifacts=state.get("artifacts") or {},
            trace=state.get("trace") or [],
            reflection_log=state.get("reflection_log") or [],
            pending_write=state.get("pending_write"),
            intent=state.get("intent", ""),
            plan=state.get("plan") or {},
            retries=int(state.get("retries") or 0),
        )


class AgentService:
    def __init__(self, session_id: str):
        self.session_id = session_id
        from src.core.storage import get_storage
        self.storage = get_storage()
        self.storage.ensure_session(session_id)
        self.wm = get_working_memory()

    # ------------------------------------------------------------------
    @property
    def workbook_id(self) -> str | None:
        return self.storage.get_active_workbook(self.session_id)

    def set_workbook(self, workbook_id: str | None) -> None:
        self.storage.set_active_workbook(self.session_id, workbook_id)
        self.wm.set(self.session_id, "active_workbook", workbook_id)
        if workbook_id:
            names = WorkbookManager.instance().list_sheet_names(workbook_id)
            if names:
                self.wm.set(self.session_id, "active_sheet", names[0])

    def toolkit(self) -> Toolkit:
        return Toolkit(workbook_id=self.workbook_id or "", session_id=self.session_id)

    # ------------------------------------------------------------------
    # 对话
    # ------------------------------------------------------------------
    def chat(self, user_input: str, mode: str = "m2", events=None) -> TurnResult:
        if not self.workbook_id:
            # 无工作簿时也允许寒暄，但数据类问题会在 plan 里转澄清
            pass
        # 会话标题：首条用户消息（截断 30 字），并刷新更新时间
        row = self.storage.get_session(self.session_id)
        title = (row["title"] if row else None) or (user_input.strip()[:30] or "未命名会话")
        self.storage.update_session(self.session_id, title=title)

        summary, recent = self._load_context()
        state = run_turn(self.session_id, self.workbook_id, user_input,
                         mode=mode, chat_summary=summary, recent_turns=recent,
                         events=events)
        result = TurnResult.from_state(state)

        # 会话级图表累积：追问（本轮不出图）不会冲掉之前生成的图表
        all_charts = list(self.wm.get(self.session_id, "session_charts", []) or [])
        for path in (result.artifacts.get("charts") or []):
            if path not in all_charts:
                all_charts.append(path)
        if all_charts:
            self.wm.set(self.session_id, "session_charts", all_charts)
        result.artifacts["all_charts"] = all_charts

        # 待确认写操作登记到 L2（跨请求存活，供确认按钮使用）
        if result.pending_write:
            self.wm.set(self.session_id, "pending_write", result.pending_write)

        # 完整聊天记录落库（供会话列表/回放），meta 保存本轮可追溯信息
        self.storage.append_chat(self.session_id, "user", user_input)
        self.storage.append_chat(self.session_id, "assistant", result.response, {
            "intent": result.intent,
            "retries": result.retries,
            "reflection_log": result.reflection_log,
            "trace": result.trace[:8],
            "charts": result.artifacts.get("charts") or [],
            "res_ids": result.artifacts.get("res_ids") or [],
        })
        self._append_history(user_input, result)
        self.wm.set(self.session_id, "last_intent", result.intent)
        self._remember_facts(user_input, result)
        return result

    def _load_context(self) -> tuple[str, list[dict]]:
        history = self.storage.kv_get(f"session:{self.session_id}", "history", []) or []
        summary = self.storage.kv_get(f"session:{self.session_id}", "l1_summary", "") or ""
        recent = history[-4:]
        return summary, recent

    def _append_history(self, user_input: str, result: TurnResult) -> None:
        scope = f"session:{self.session_id}"
        history = self.storage.kv_get(scope, "history", []) or []
        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": result.response[:800]})
        # L1 压缩：超阈值时把旧一半滚动摘要
        if len(history) > L1_COMPRESS_THRESHOLD:
            old = history[: len(history) // 2]
            history = history[len(history) // 2:]
            old_text = "\n".join(f"{'用户' if m['role'] == 'user' else '助手'}：{m['content'][:200]}"
                                 for m in old)
            prev_summary = self.storage.kv_get(scope, "l1_summary", "") or ""
            try:
                model = get_chat_model(temperature=0.0)
                from langchain_core.messages import HumanMessage, SystemMessage
                resp = model.invoke([
                    SystemMessage(content="把对话历史压缩成要点摘要（≤150字），保留：使用的表、"
                                          "查过的指标、做过的修改、用户的偏好。"),
                    HumanMessage(content=f"已有摘要：{prev_summary}\n\n新增对话：\n{old_text}"),
                ])
                text = resp.content if isinstance(resp.content, str) else str(resp.content)
            except Exception as e:  # noqa: BLE001
                logger.warning("L1 压缩失败（保留截断摘要）：%s", e)
                text = (prev_summary + "\n" + old_text)[:400]
            self.storage.kv_set(scope, "l1_summary", text)
        self.storage.kv_set(scope, "history", history[-40:])

    def _remember_facts(self, user_input: str, result: TurnResult) -> None:
        """值得记住的事实 → Mem0（session 作用域；显式偏好 → user 作用域）。"""
        from src.memory.longterm import get_longterm_memory
        ltm = get_longterm_memory()
        facts = []
        if result.intent in ("simple_query", "report", "diagnose", "forecast", "complex"):
            facts.append(f"用户在会话中问过：{user_input[:80]}")
        if "偏好" in user_input or "以后" in user_input or "默认" in user_input:
            ltm.add_user_preference(user_input[:120])
        if result.plan.get("reasoning"):
            pass
        ltm.add_session_fact(self.session_id, facts)

    # ------------------------------------------------------------------
    # 写确认流（人在回路，规格 §3.3 / FR-9）
    # ------------------------------------------------------------------
    def confirm_write(self) -> TurnResult:
        """确认执行待定写操作。影响 >50 单元格时第一次确认只提示二次确认。"""
        pending = self.wm.get(self.session_id, "pending_write")
        if not pending:
            return TurnResult(response="当前没有待确认的修改。")
        stage = int(pending.get("stage") or 1)
        if pending.get("needs_double") and stage < 2:
            pending["stage"] = 2
            self.wm.set(self.session_id, "pending_write", pending)
            return TurnResult(
                response=f"该操作将影响 **{pending['n_cells']} 个单元格**（超过 50）。\n\n"
                         f"**{pending['summary']}**\n\n确认无误请再次点击「确认执行」。",
                confirm_required=True, pending_write=pending)

        manager = WorkbookManager.instance()
        op = pending["op"]
        plan_result = manager.plan_write(self.workbook_id, WriteOp(
            op_type=op["op_type"], sheet=op["sheet"], params=op.get("params") or {},
            description=op.get("description", "")))
        applied = manager.apply_write(self.workbook_id, plan_result)
        self.wm.set(self.session_id, "pending_write", None)
        self.wm.set(self.session_id, "last_intent", "write_applied")
        self.storage.append_chat(self.session_id, "assistant",
                                 f"已执行：{applied['description']}",
                                 {"intent": "write_applied"})
        return TurnResult(
            response=f"已执行：{applied['description']}\n\n"
                     f"（版本 v{applied['version']}，影响 {applied['n_cells']} 个单元格，"
                     f"可在左侧「版本历史」撤销）",
            artifacts={"applied": applied},
        )

    def cancel_write(self) -> TurnResult | None:
        """取消待定写操作；没有待确认修改时不写入任何会话记录。"""
        if not self.wm.get(self.session_id, "pending_write"):
            return None
        self.wm.set(self.session_id, "pending_write", None)
        self.storage.append_chat(self.session_id, "assistant",
                                 "已取消该修改，数据未做任何变更。",
                                 {"intent": "write_cancelled"})
        return TurnResult(response="已取消该修改，数据未做任何变更。")

    # ------------------------------------------------------------------
    # 版本操作（代理给 WorkbookManager）
    # ------------------------------------------------------------------
    def _log_action(self, text: str, intent: str = "version_action") -> None:
        """版本/快照操作也写入会话记录，保证回放完整。"""
        self.storage.append_chat(self.session_id, "assistant", text, {"intent": intent})

    def undo(self) -> str:
        msg = WorkbookManager.instance().undo(self.workbook_id)
        self._invalidate_result_cache()
        self._log_action(msg)
        return msg

    def redo(self) -> str:
        msg = WorkbookManager.instance().redo(self.workbook_id)
        self._invalidate_result_cache()
        self._log_action(msg)
        return msg

    def goto(self, n: int) -> str:
        msg = WorkbookManager.instance().goto(self.workbook_id, n)
        self._invalidate_result_cache()
        self._log_action(msg)
        return msg

    def save_snapshot(self, name: str) -> str:
        msg = WorkbookManager.instance().save_snapshot(self.workbook_id, name)
        self._log_action(msg)
        return msg

    def restore_snapshot(self, name: str) -> str:
        msg = WorkbookManager.instance().restore_snapshot(self.workbook_id, name)
        self._invalidate_result_cache()
        self._log_action(msg)
        return msg

    def clear_conversation(self) -> None:
        """清空当前会话的对话（消息、L1 摘要、工作记忆引用、会话图表与标题）。"""
        self.storage.clear_session_chat(self.session_id)
        scope = f"session:{self.session_id}"
        for key in ("history", "l1_summary", "session_charts", "last_query",
                    "last_result_ref", "last_intent", "pending_write"):
            self.storage.kv_delete(scope, key)
        self.storage.update_session(self.session_id, title="")

    def delete_session(self, session_id: str) -> None:
        self.storage.delete_session(session_id)

    def _invalidate_result_cache(self) -> None:
        WorkbookManager.instance()._df_cache.clear()

    # ------------------------------------------------------------------
    # 数据接入（UI 用）
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 会话列表 / 回放
    # ------------------------------------------------------------------
    def list_sessions(self, limit: int = 30) -> list[dict]:
        return [dict(r) for r in self.storage.list_sessions(limit)]

    def session_messages(self, session_id: str | None = None) -> list[dict]:
        return self.storage.list_chat(session_id or self.session_id)

    def load_file(self, path: str, display_name: str | None = None,
                  header_rows: dict[str, int] | None = None) -> tuple[str, list[str]]:
        wb_id, warnings = WorkbookManager.instance().create_from_import(
            path, display_name=display_name, header_rows=header_rows)
        self.set_workbook(wb_id)
        return wb_id, warnings
