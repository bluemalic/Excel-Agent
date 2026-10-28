"""L2 工作记忆（规格 §5：Working Memory，session 作用域）。

确定性指代消解的锚点：active_sheet / active_workbook / last_query / last_result_ref。
主图 resolve_l2 节点先查这里，把"它/刚才/华南呢"落成明确对象；
同批事实同时写入 Mem0 session 作用域做语义检索（见 longterm.py）。
"""

from __future__ import annotations

import threading

from src.core.storage import get_storage

_VALID_KEYS = {"active_workbook", "active_sheet", "last_query", "last_result_ref",
               "last_intent", "pending_write", "last_report", "session_charts"}


class WorkingMemory:
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self.storage = get_storage()

    @classmethod
    def instance(cls) -> "WorkingMemory":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @staticmethod
    def _scope(session_id: str) -> str:
        return f"session:{session_id}"

    def set(self, session_id: str, key: str, value) -> None:
        if key not in _VALID_KEYS:
            raise ValueError(f"未登记的工作记忆键：{key}（允许 {_VALID_KEYS}）")
        self.storage.kv_set(self._scope(session_id), key, value)

    def get(self, session_id: str, key: str, default=None):
        return self.storage.kv_get(self._scope(session_id), key, default)

    def snapshot(self, session_id: str) -> dict:
        return self.storage.kv_all(self._scope(session_id))

    def to_prompt(self, session_id: str) -> str:
        """渲染给 LLM 的工作记忆上下文（轻量、结构化）。"""
        snap = self.snapshot(session_id)
        if not snap:
            return "（工作记忆为空：这是本会话第一条消息或尚未选择数据）"
        lines = []
        if snap.get("active_workbook"):
            lines.append(f"当前工作簿：{snap['active_workbook']}")
        if snap.get("active_sheet"):
            lines.append(f"当前工作表：{snap['active_sheet']}")
        if snap.get("last_query"):
            lines.append(f"上一条查询 SQL：{snap['last_query']}")
        if snap.get("last_result_ref"):
            lines.append(f"上一次结果编号：{snap['last_result_ref']}（用户说\"刚才/它\"时指这个）")
        if snap.get("last_intent"):
            lines.append(f"上一轮意图：{snap['last_intent']}")
        return "\n".join(lines) if lines else "（工作记忆为空）"


def get_working_memory() -> WorkingMemory:
    return WorkingMemory.instance()
