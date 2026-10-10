"""进程内事件总线：SSE /api/events 端点的数据源。

发布方（scheduler 事件、数据版本监听线程）调用 publish；
订阅方经 subscribe 获得 Subscription：
  - 线程消费方用 sub.q（queue.Queue）
  - SSE 端点在异步循环中设置 sub.loop + sub.aq，publish 经
    call_soon_threadsafe 桥接进事件循环，避免阻塞异步路径。
"""

from __future__ import annotations

import asyncio
import queue
import threading
from typing import Any

_MAX_SUBSCRIBERS = 16

_lock = threading.Lock()
_subscribers: set[Subscription] = set()


class Subscription:
    def __init__(self, maxsize: int = 64) -> None:
        self.q: queue.Queue = queue.Queue(maxsize)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.aq: asyncio.Queue | None = None


def _put_thread(q: queue.Queue, event: dict[str, Any]) -> None:
    try:
        q.put_nowait(event)
    except queue.Full:
        pass


def _put_async(aq: asyncio.Queue, event: dict[str, Any]) -> None:
    try:
        aq.put_nowait(event)
    except asyncio.QueueFull:
        pass


def publish(event: dict[str, Any]) -> None:
    with _lock:
        subs = list(_subscribers)
    for sub in subs:
        if sub.loop is not None and sub.aq is not None:
            try:
                sub.loop.call_soon_threadsafe(_put_async, sub.aq, event)
                continue
            except RuntimeError:
                sub.loop = None
        _put_thread(sub.q, event)


def subscribe(maxsize: int = 64) -> Subscription:
    sub = Subscription(maxsize)
    with _lock:
        if len(_subscribers) >= _MAX_SUBSCRIBERS:
            raise RuntimeError("sse subscriber limit reached")
        _subscribers.add(sub)
    return sub


def unsubscribe(sub: Subscription) -> None:
    with _lock:
        _subscribers.discard(sub)
