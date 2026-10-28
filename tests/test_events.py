"""运行事件总线单元测试：阶段/逐字事件、取消标记与上下文绑定。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.agents.events import AgentEvents, TurnCancelled, bind, check_cancel, current_events, emit, unbind


def test_emit_and_drain():
    ev = AgentEvents()
    ev.emit("stage", "规划任务")
    ev.emit("token", "你好")
    items = ev.drain()
    assert [i["kind"] for i in items] == ["stage", "token"]
    assert items[1]["text"] == "你好"
    assert ev.empty()
    assert not ev.cancelled()


def test_cancel_flag():
    ev = AgentEvents()
    ev.cancel()
    assert ev.cancelled()


def test_context_binding_and_cancel_raises():
    ev = AgentEvents()
    token = bind(ev)
    try:
        assert current_events() is ev
        emit("stage", "执行计划")
        check_cancel()  # 未取消，不抛
    finally:
        unbind(token)
    ev.cancel()
    token = bind(ev)
    try:
        with pytest.raises(TurnCancelled):
            check_cancel()
    finally:
        unbind(token)
    assert ev.drain()[0]["text"] == "执行计划"
