"""L3 长期偏好记忆（Mem0 自托管 OSS，user 作用域）+ Mem0 session 事实检索。

配置（规格 §5）：
- LLM：openai provider，读 .env（OPENAI_API_KEY / OPENAI_BASE_URL → DeepSeek）；
- Embedder：本地 HuggingFace 模型（sentence-transformers，无需外部 API）；
- 向量库：qdrant 本地模式（path=数据目录），零 Docker。

降级策略（已与用户确认）：Mem0 初始化或调用失败时 L3 自动关闭并记录原因，
L1（LangGraph checkpoint）与 L2（工作记忆）完全不受影响；可通过
memory.l3_enabled() 查询当前状态，UI 明示降级。
"""

from __future__ import annotations

import logging
import threading

from src import config

logger = logging.getLogger(__name__)

_DEFAULT_USER = "local_user"
_EMBED_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"   # 中文友好，~470MB 之内


class LongTermMemory:
    """Mem0 封装；初始化失败时 available=False，所有调用安全返回空。"""

    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self.available = False
        self.disabled_reason = ""
        self._mem = None
        if not config.llm_configured():
            self.disabled_reason = "未配置 OPENAI_API_KEY（填好 .env 后重启即启用）"
            return
        try:
            self._init_mem0()
            self.available = True
        except Exception as e:  # noqa: BLE001 —— 降级不阻断主流程
            self.disabled_reason = f"{type(e).__name__}: {e}"
            logger.warning("Mem0 初始化失败，L3 长期记忆降级关闭：%s", self.disabled_reason)

    def _init_mem0(self) -> None:
        import os

        # 必须在导入 mem0 之前设置：mem0 在模块导入时读取 MEM0_DIR。
        os.environ.setdefault("MEM0_DIR", str(config.MEM0_PATH / "home"))
        from mem0 import Memory

        cfg = {
            "llm": {
                "provider": "openai",
                "config": {
                    "model": config.OPENAI_MODEL,
                    "temperature": 0.1,
                    "api_key": config.OPENAI_API_KEY,
                    "openai_base_url": config.OPENAI_BASE_URL,
                },
            },
            "embedder": {
                "provider": "huggingface",
                "config": {"model": _EMBED_MODEL},
            },
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": "excel_agent_mem0",
                    "path": str(config.MEM0_PATH),
                    # 本地模型 paraphrase-multilingual-MiniLM-L12-v2 输出 384 维；
                    # 必须显式声明，否则 mem0 默认按 1536 维建集合导致维度不匹配
                    "embedding_model_dims": 384,
                },
            },
            "history_db_path": str(config.MEM0_PATH / "history.db"),
            "version": "v1.1",
        }
        self._mem = Memory.from_config(cfg)

    @classmethod
    def instance(cls) -> "LongTermMemory":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ------------------------------------------------------------------
    def add_session_fact(self, session_id: str, facts: list[str]) -> None:
        """把会话内值得长期记住的事实写入 Mem0（run_id=会话，实现 session 作用域）。"""
        if not (self.available and facts):
            return
        try:
            for fact in facts:
                self._mem.add(fact, user_id=_DEFAULT_USER, run_id=session_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("Mem0 add 失败（已忽略）：%s", e)

    def add_user_preference(self, text: str) -> None:
        """用户显式偏好（"以后都用万元为单位"）→ user 作用域。"""
        if not (self.available and text):
            return
        try:
            self._mem.add(text, user_id=_DEFAULT_USER)
        except Exception as e:  # noqa: BLE001
            logger.warning("Mem0 偏好写入失败（已忽略）：%s", e)

    def search(self, query: str, session_id: str | None = None, limit: int = 5) -> list[str]:
        if not self.available:
            return []
        try:
            # Mem0 v2：实体作用域必须走 filters（顶层 user_id 已废弃）
            raw = self._mem.search(query, filters={"user_id": _DEFAULT_USER},
                                   top_k=limit)
            results = raw.get("results", raw) if isinstance(raw, dict) else raw
            out = []
            for r in results or []:
                if not isinstance(r, dict):
                    out.append(str(r))
                    continue
                meta = r.get("metadata") or {}
                # session 作用域的记忆只回给同会话
                if session_id and meta.get("run_id") and meta.get("run_id") != session_id:
                    continue
                if r.get("memory"):
                    out.append(r["memory"])
            return out
        except Exception as e:  # noqa: BLE001
            logger.warning("Mem0 search 失败（已忽略）：%s", e)
            return []

    def list_all(self, limit: int = 30) -> list[dict]:
        """列出当前用户的长期记忆（供 UI 记忆面板展示）。"""
        if not self.available:
            return []
        try:
            raw = self._mem.get_all(filters={"user_id": _DEFAULT_USER}, top_k=limit)
            results = raw.get("results", raw) if isinstance(raw, dict) else raw
            out: list[dict] = []
            for r in results or []:
                if isinstance(r, dict):
                    meta = r.get("metadata") or {}
                    out.append({"memory": r.get("memory", ""), "run_id": meta.get("run_id")})
                else:
                    out.append({"memory": str(r), "run_id": None})
            return out
        except Exception as e:  # noqa: BLE001
            logger.warning("Mem0 get_all 失败（已忽略）：%s", e)
            return []

    def status(self) -> dict:
        return {"available": self.available, "reason": self.disabled_reason,
                "embed_model": _EMBED_MODEL}


def get_longterm_memory() -> LongTermMemory:
    return LongTermMemory.instance()
