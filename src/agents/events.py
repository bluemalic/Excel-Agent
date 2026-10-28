"""Agent 运行时事件总线：流式阶段/逐字输出 + 可中断执行。

Node 通过 emit()/check_cancel() 汇报执行进展；UI 在另一个线程中轮询事件队列，
实时更新对话内容并提供“停止”按钮。评测/命令行直接调用 service.chat 时
不传 events，行为与之前完全一致（无流式开销）。
"""

from __future__ import annotations

import contextvars
import queue
import threading
from dataclasses import dataclass, field


class TurnCancelled(Exception):
    """用户点击“停止”后，在下一个可中断点抛出。"""


@dataclass
class AgentEvents:
    """一轮对话的事件队列与取消标记（每个会话/每轮一个）。"""

    q: "queue.Queue[dict]" = field(default_factory=queue.Queue)
    _cancel: threading.Event = field(default_factory=threading.Event)

    def emit(self, kind: str, text: str = "", **extra) -> None:
        self.q.put({"kind": kind, "text": text, **extra})

    def cancel(self) -> None:
        self._cancel.set()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def drain(self) -> list[dict]:
        items: list[dict] = []
        while True:
            try:
                items.append(self.q.get_nowait())
            except queue.Empty:
                return items

    def empty(self) -> bool:
        return self.q.empty()


_CURRENT: contextvars.ContextVar[AgentEvents | None] = contextvars.ContextVar(
    "excel_agent_events", default=None)


def bind(events: AgentEvents | None):
    """绑定到当前执行上下文，返回可用于 reset 的 token。"""
    return _CURRENT.set(events)


def unbind(token) -> None:
    try:
        _CURRENT.reset(token)
    except ValueError:
        pass


def current_events() -> AgentEvents | None:
    return _CURRENT.get()


def emit(kind: str, text: str = "", **extra) -> None:
    ev = _CURRENT.get()
    if ev is not None:
        ev.emit(kind, text, **extra)


def check_cancel() -> None:
    ev = _CURRENT.get()
    if ev is not None and ev.cancelled():
        raise TurnCancelled()
