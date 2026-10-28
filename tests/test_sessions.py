"""会话持久化/清理/删除单元测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.storage import Storage


def test_chat_log_and_session_list(tmp_path):
    s = Storage(db_path=tmp_path / "m.sqlite")
    s.ensure_session("s1")
    s.update_session("s1", title="销售分析")
    s.append_chat("s1", "user", "各大区总和？")
    s.append_chat("s1", "assistant", "华东最高", {"intent": "simple_query"})
    rows = [dict(r) for r in s.list_sessions()]
    assert rows[0]["session_id"] == "s1"
    assert rows[0]["title"] == "销售分析"
    assert rows[0]["n_messages"] == 2
    msgs = s.list_chat("s1")
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["meta"]["intent"] == "simple_query"


def test_clear_and_delete_session(tmp_path):
    s = Storage(db_path=tmp_path / "m.sqlite")
    s.ensure_session("s1")
    s.append_chat("s1", "user", "你好")
    s.kv_set("session:s1", "history", [{"role": "user", "content": "你好"}])
    s.clear_session_chat("s1")
    assert s.list_chat("s1") == []
    s.kv_delete("session:s1", "history")
    assert s.kv_get("session:s1", "history", None) is None

    s.append_chat("s1", "user", "再来")
    s.delete_session("s1")
    assert s.get_session("s1") is None
    assert s.list_chat("s1") == []
    assert s.kv_all("session:s1") == {}


def test_sessions_hidden_when_empty(tmp_path):
    s = Storage(db_path=tmp_path / "m.sqlite")
    s.ensure_session("empty")
    assert s.list_sessions() == []
    s.append_chat("empty", "user", "hi")
    assert len(s.list_sessions()) == 1
