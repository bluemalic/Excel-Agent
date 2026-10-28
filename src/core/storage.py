"""SQLite 元数据存储：workbook/sheet/版本链/操作日志(含逆操作)/样式/会话/查询结果引用。

设计要点：
- 数据本体一律存 Parquet 文件，SQLite 只存元数据与指针。
- 版本链：每 sheet 一条 v0..vK 序列，v0 为导入初始态；写操作追加新版本并前移指针。
- ops 表即逆操作日志：每条写操作记录 params + inverse + 前后版本号，撤销/审计都靠它。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

from src import config


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _json_default(obj):
    """JSON 兜底序列化：pandas Timestamp / numpy 标量 / date 等（逆操作日志里可能带日期值）。"""
    import datetime as _dt

    try:
        import numpy as _np
        if isinstance(obj, _np.integer):
            return int(obj)
        if isinstance(obj, _np.floating):
            return None if _np.isnan(obj) else float(obj)
        if isinstance(obj, _np.bool_):
            return bool(obj)
    except ImportError:
        pass
    if isinstance(obj, (_dt.datetime, _dt.date)):
        return obj.isoformat()
    try:
        import pandas as _pd
        if isinstance(obj, _pd.Timestamp):
            return obj.isoformat()
    except ImportError:
        pass
    if hasattr(obj, "item"):
        try:
            return obj.item()
        except (ValueError, TypeError):
            pass
    return str(obj)


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=_json_default)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS workbooks (
  workbook_id   TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  origin        TEXT NOT NULL,              -- upload / sample / template / join
  original_path TEXT,                       -- 原始文件（v0 字节保底，用于 AC-5）
  cursor        INTEGER NOT NULL DEFAULT 0, -- 已应用操作数（撤销/重做指针）
  created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sheets (
  workbook_id     TEXT NOT NULL,
  sheet_name      TEXT NOT NULL,
  origin          TEXT NOT NULL DEFAULT 'import',  -- import / op（由 create_sheet 操作创建）
  n_rows          INTEGER NOT NULL DEFAULT 0,
  n_cols          INTEGER NOT NULL DEFAULT 0,
  current_version INTEGER NOT NULL DEFAULT 0,
  max_version     INTEGER NOT NULL DEFAULT 0,
  active          INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (workbook_id, sheet_name)
);
CREATE TABLE IF NOT EXISTS versions (
  workbook_id  TEXT NOT NULL,
  sheet_name   TEXT NOT NULL,
  version      INTEGER NOT NULL,
  parquet_path TEXT NOT NULL,
  columns_json TEXT NOT NULL,               -- 该版本的列 schema（供历史回溯）
  description  TEXT,
  created_at   TEXT NOT NULL,
  PRIMARY KEY (workbook_id, sheet_name, version)
);
CREATE TABLE IF NOT EXISTS ops (
  op_id          TEXT PRIMARY KEY,
  workbook_id    TEXT NOT NULL,
  sheet_name     TEXT NOT NULL,
  seq            INTEGER NOT NULL,          -- workbook 内全局递增
  op_type        TEXT NOT NULL,
  params_json    TEXT NOT NULL,
  inverse_json   TEXT NOT NULL,
  before_version INTEGER NOT NULL,
  after_version  INTEGER NOT NULL,
  description    TEXT,
  created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS styles (
  workbook_id TEXT NOT NULL,
  sheet_name  TEXT NOT NULL,
  cell_ref    TEXT NOT NULL,                -- 'A1' / 'A1:C10' / 'col:列名'
  style_json  TEXT NOT NULL,
  PRIMARY KEY (workbook_id, sheet_name, cell_ref)
);
CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id      TEXT PRIMARY KEY,
  workbook_id      TEXT NOT NULL,
  name             TEXT NOT NULL,
  version_map_json TEXT NOT NULL,           -- {sheet: version}
  created_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_kv (
  scope      TEXT NOT NULL,                 -- 'session:<sid>' / 'user:<uid>'
  key        TEXT NOT NULL,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (scope, key)
);
CREATE TABLE IF NOT EXISTS sessions (
  session_id         TEXT PRIMARY KEY,
  created_at         TEXT NOT NULL,
  active_workbook_id TEXT,
  title              TEXT,
  updated_at         TEXT
);
CREATE TABLE IF NOT EXISTS chat_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  TEXT NOT NULL,
  role        TEXT NOT NULL,
  content     TEXT NOT NULL,
  meta_json   TEXT,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_log_session ON chat_log(session_id, id);
CREATE TABLE IF NOT EXISTS result_refs (
  res_id       TEXT PRIMARY KEY,
  session_id   TEXT,
  workbook_id  TEXT,
  sheet_name   TEXT,
  sql_text     TEXT,
  columns_json TEXT,
  rows_json    TEXT,                        -- 全量结果（≤5000 行）
  total_rows   INTEGER,
  created_at   TEXT NOT NULL
);
"""


class Storage:
    """线程安全的 SQLite 封装（应用内单例）。"""

    def __init__(self, db_path: Path | None = None):
        config.ensure_dirs()
        self._db_path = Path(db_path) if db_path else config.DB_PATH
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """旧库结构迁移（会话标题/更新时间）。"""
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(sessions)")}
        if "title" not in cols:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
        if "updated_at" not in cols:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN updated_at TEXT")
            self._conn.execute("UPDATE sessions SET updated_at=created_at")

    # ---------------- 基础 ----------------
    def execute(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = cur.fetchall()
            self._conn.commit()
            return rows

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------- workbook ----------------
    def create_workbook(self, name: str, origin: str, original_path: str | None) -> str:
        wb_id = _uid("wb")
        self.execute(
            "INSERT INTO workbooks VALUES (?,?,?,?,?,?)",
            (wb_id, name, origin, original_path, 0, _now()),
        )
        return wb_id

    def set_cursor(self, wb_id: str, cursor: int) -> None:
        self.execute("UPDATE workbooks SET cursor=? WHERE workbook_id=?", (cursor, wb_id))

    def get_cursor(self, wb_id: str) -> int:
        rows = self.execute("SELECT cursor FROM workbooks WHERE workbook_id=?", (wb_id,))
        return int(rows[0]["cursor"]) if rows else 0

    def get_workbook(self, wb_id: str) -> sqlite3.Row | None:
        rows = self.execute("SELECT * FROM workbooks WHERE workbook_id=?", (wb_id,))
        return rows[0] if rows else None

    def list_workbooks(self) -> list[sqlite3.Row]:
        return self.execute("SELECT * FROM workbooks ORDER BY created_at DESC")

    # ---------------- sheet ----------------
    def upsert_sheet(
        self, wb_id: str, sheet: str, n_rows: int, n_cols: int,
        current_version: int, max_version: int, active: int = 1,
        origin: str = "import",
    ) -> None:
        self.execute(
            "INSERT INTO sheets VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(workbook_id, sheet_name) DO UPDATE SET "
            "n_rows=excluded.n_rows, n_cols=excluded.n_cols, "
            "current_version=excluded.current_version, max_version=excluded.max_version, "
            "active=excluded.active, origin=excluded.origin",
            (wb_id, sheet, origin, n_rows, n_cols, current_version, max_version, active),
        )

    def get_sheet(self, wb_id: str, sheet: str) -> sqlite3.Row | None:
        rows = self.execute(
            "SELECT * FROM sheets WHERE workbook_id=? AND sheet_name=?", (wb_id, sheet)
        )
        return rows[0] if rows else None

    def list_sheets(self, wb_id: str, only_active: bool = True) -> list[sqlite3.Row]:
        sql = "SELECT * FROM sheets WHERE workbook_id=?"
        if only_active:
            sql += " AND active=1"
        return self.execute(sql + " ORDER BY rowid", (wb_id,))

    # ---------------- 版本 ----------------
    def add_version(
        self, wb_id: str, sheet: str, version: int, parquet_path: str,
        columns_json: str, description: str,
    ) -> None:
        self.execute(
            "INSERT OR REPLACE INTO versions VALUES (?,?,?,?,?,?,?)",
            (wb_id, sheet, version, parquet_path, columns_json, description, _now()),
        )

    def get_version(self, wb_id: str, sheet: str, version: int) -> sqlite3.Row | None:
        rows = self.execute(
            "SELECT * FROM versions WHERE workbook_id=? AND sheet_name=? AND version=?",
            (wb_id, sheet, version),
        )
        return rows[0] if rows else None

    def list_versions(self, wb_id: str, sheet: str) -> list[sqlite3.Row]:
        return self.execute(
            "SELECT * FROM versions WHERE workbook_id=? AND sheet_name=? ORDER BY version",
            (wb_id, sheet),
        )

    # ---------------- 操作日志（逆操作）----------------
    def next_seq(self, wb_id: str) -> int:
        rows = self.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM ops WHERE workbook_id=?", (wb_id,)
        )
        return int(rows[0]["m"]) + 1

    def add_op(
        self, wb_id: str, sheet: str, op_type: str, params: dict, inverse: dict,
        before_version: int, after_version: int, description: str,
    ) -> str:
        op_id = _uid("op")
        self.execute(
            "INSERT INTO ops VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (op_id, wb_id, sheet, self.next_seq(wb_id), op_type,
             _dumps(params), _dumps(inverse),
             before_version, after_version, description, _now()),
        )
        return op_id

    def list_ops(self, wb_id: str) -> list[sqlite3.Row]:
        return self.execute(
            "SELECT * FROM ops WHERE workbook_id=? ORDER BY seq", (wb_id,)
        )

    def get_op(self, op_id: str) -> sqlite3.Row | None:
        rows = self.execute("SELECT * FROM ops WHERE op_id=?", (op_id,))
        return rows[0] if rows else None

    # ---------------- 样式 ----------------
    def set_style(self, wb_id: str, sheet: str, cell_ref: str, style: dict) -> None:
        self.execute(
            "INSERT INTO styles VALUES (?,?,?,?) "
            "ON CONFLICT(workbook_id, sheet_name, cell_ref) DO UPDATE SET style_json=excluded.style_json",
            (wb_id, sheet, cell_ref, _dumps(style)),
        )

    def get_styles(self, wb_id: str, sheet: str) -> dict[str, dict]:
        rows = self.execute(
            "SELECT cell_ref, style_json FROM styles WHERE workbook_id=? AND sheet_name=?",
            (wb_id, sheet),
        )
        return {r["cell_ref"]: json.loads(r["style_json"]) for r in rows}

    def delete_styles(self, wb_id: str, sheet: str, cell_refs: list[str]) -> None:
        for ref in cell_refs:
            self.execute(
                "DELETE FROM styles WHERE workbook_id=? AND sheet_name=? AND cell_ref=?",
                (wb_id, sheet, ref),
            )

    # ---------------- 快照 ----------------
    def add_snapshot(self, wb_id: str, name: str, version_map: dict[str, int]) -> str:
        sid = _uid("snap")
        self.execute(
            "INSERT INTO snapshots VALUES (?,?,?,?,?)",
            (sid, wb_id, name, _dumps(version_map), _now()),
        )
        return sid

    def get_snapshot(self, wb_id: str, name: str) -> sqlite3.Row | None:
        rows = self.execute(
            "SELECT * FROM snapshots WHERE workbook_id=? AND name=? ORDER BY created_at DESC",
            (wb_id, name),
        )
        return rows[0] if rows else None

    def list_snapshots(self, wb_id: str) -> list[sqlite3.Row]:
        return self.execute(
            "SELECT * FROM snapshots WHERE workbook_id=? ORDER BY created_at DESC", (wb_id,)
        )

    # ---------------- 会话 ----------------
    def ensure_session(self, session_id: str) -> None:
        rows = self.execute("SELECT 1 FROM sessions WHERE session_id=?", (session_id,))
        if not rows:
            self.execute(
                "INSERT INTO sessions(session_id, created_at, active_workbook_id, updated_at) "
                "VALUES (?,?,NULL,?)", (session_id, _now(), _now()))

    def update_session(self, session_id: str, title: str | None = None,
                       updated_at: str | None = None) -> None:
        self.ensure_session(session_id)
        sets, params = [], []
        if title is not None:
            sets.append("title=?")
            params.append(title)
        sets.append("updated_at=?")
        params.append(updated_at or _now())
        params.append(session_id)
        self.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE session_id=?", tuple(params))

    def clear_session_chat(self, session_id: str) -> None:
        self.execute("DELETE FROM chat_log WHERE session_id=?", (session_id,))

    def delete_session(self, session_id: str) -> None:
        self.execute("DELETE FROM chat_log WHERE session_id=?", (session_id,))
        self.execute("DELETE FROM result_refs WHERE session_id=?", (session_id,))
        self.execute("DELETE FROM memory_kv WHERE scope=?", (f"session:{session_id}",))
        self.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))

    def get_session(self, session_id: str) -> sqlite3.Row | None:
        rows = self.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,))
        return rows[0] if rows else None

    def list_sessions(self, limit: int = 30) -> list[sqlite3.Row]:
        """最近会话（按更新时间倒序）；过滤掉从未产生消息且无工作簿的空会话。"""
        return self.execute(
            """SELECT s.session_id, s.title, s.created_at, s.updated_at,
                      s.active_workbook_id, w.name AS workbook_name,
                      (SELECT COUNT(*) FROM chat_log c WHERE c.session_id=s.session_id)
                        AS n_messages
               FROM sessions s LEFT JOIN workbooks w
                    ON w.workbook_id = s.active_workbook_id
               WHERE (SELECT COUNT(*) FROM chat_log c2 WHERE c2.session_id=s.session_id) > 0
                  OR s.active_workbook_id IS NOT NULL
               ORDER BY COALESCE(s.updated_at, s.created_at) DESC
               LIMIT ?""", (limit,))

    # ---------------- 会话聊天记录 ----------------
    def append_chat(self, session_id: str, role: str, content: str,
                    meta: dict | None = None) -> None:
        self.execute(
            "INSERT INTO chat_log(session_id, role, content, meta_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (session_id, role, content, _dumps(meta) if meta else None, _now()))

    def list_chat(self, session_id: str) -> list[dict]:
        rows = self.execute(
            "SELECT role, content, meta_json, created_at FROM chat_log "
            "WHERE session_id=? ORDER BY id", (session_id,))
        out = []
        for r in rows:
            try:
                meta = json.loads(r["meta_json"]) if r["meta_json"] else None
            except json.JSONDecodeError:
                meta = None
            out.append({"role": r["role"], "content": r["content"],
                        "meta": meta, "created_at": r["created_at"]})
        return out

    def set_active_workbook(self, session_id: str, wb_id: str | None) -> None:
        self.ensure_session(session_id)
        self.execute(
            "UPDATE sessions SET active_workbook_id=? WHERE session_id=?", (wb_id, session_id)
        )

    def get_active_workbook(self, session_id: str) -> str | None:
        rows = self.execute(
            "SELECT active_workbook_id FROM sessions WHERE session_id=?", (session_id,)
        )
        return rows[0]["active_workbook_id"] if rows else None

    # ---------------- 记忆 kv（L2 工作记忆）----------------
    def kv_set(self, scope: str, key: str, value) -> None:
        self.execute(
            "INSERT INTO memory_kv VALUES (?,?,?,?) "
            "ON CONFLICT(scope, key) DO UPDATE SET value_json=excluded.value_json, "
            "updated_at=excluded.updated_at",
            (scope, key, _dumps(value), _now()),
        )

    def kv_get(self, scope: str, key: str, default=None):
        rows = self.execute(
            "SELECT value_json FROM memory_kv WHERE scope=? AND key=?", (scope, key)
        )
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value_json"])
        except json.JSONDecodeError:
            return default

    def kv_delete(self, scope: str, key: str) -> None:
        self.execute("DELETE FROM memory_kv WHERE scope=? AND key=?", (scope, key))

    def kv_all(self, scope: str) -> dict:
        rows = self.execute("SELECT key, value_json FROM memory_kv WHERE scope=?", (scope,))
        out = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value_json"])
            except json.JSONDecodeError:
                continue
        return out

    # ---------------- 查询结果引用（res_id，供指代消解与 chart 复用）----------------
    def save_result_ref(
        self, res_id: str, session_id: str, wb_id: str, sheet: str,
        sql_text: str, columns: list[str], rows: list[list], total_rows: int,
    ) -> None:
        self.execute(
            "INSERT OR REPLACE INTO result_refs VALUES (?,?,?,?,?,?,?,?,?)",
            (res_id, session_id, wb_id, sheet, sql_text,
             _dumps(columns),
             _dumps(rows), total_rows, _now()),
        )

    def get_result_ref(self, res_id: str) -> sqlite3.Row | None:
        rows = self.execute("SELECT * FROM result_refs WHERE res_id=?", (res_id,))
        return rows[0] if rows else None

    def get_latest_result_ref(self, session_id: str) -> sqlite3.Row | None:
        rows = self.execute(
            "SELECT * FROM result_refs WHERE session_id=? ORDER BY created_at DESC, res_id DESC LIMIT 1",
            (session_id,),
        )
        return rows[0] if rows else None


# 应用级单例
_storage: Storage | None = None
_storage_lock = threading.Lock()


def get_storage() -> Storage:
    global _storage
    with _storage_lock:
        if _storage is None:
            _storage = Storage()
        return _storage
