"""SSE 事件总线与 /api/events 端点。

注：TestClient（httpx 0.28 + starlette 0.41 portal）会缓冲整个流式响应，
无限 SSE 流无法经 TestClient 读取；端点验证改为直接调用路由函数。
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from qsim.services import events  # noqa: E402
from qsim.services.api.app import create_app  # noqa: E402


def test_bus_publish_subscribe():
    sub = events.subscribe()
    try:
        events.publish({"type": "job", "job_id": "j1", "status": "success"})
        assert sub.q.get(timeout=1)["job_id"] == "j1"
    finally:
        events.unsubscribe(sub)


def test_bus_unsubscribe_stops_delivery():
    sub = events.subscribe()
    events.unsubscribe(sub)
    events.publish({"type": "job"})
    assert sub.q.empty()


def test_sse_endpoint_format():
    app = create_app()
    endpoint = next(
        r.endpoint for r in app.routes if getattr(r, "path", "") == "/api/events"
    )
    resp = endpoint()
    assert resp.media_type == "text/event-stream"

    def publisher():
        for _ in range(20):
            events.publish({"type": "job", "job_id": "direct", "status": "ok"})
            time.sleep(0.1)

    threading.Thread(target=publisher, daemon=True).start()

    async def first_line():
        return await asyncio.wait_for(resp.body_iterator.__anext__(), timeout=10)

    line = asyncio.run(first_line())
    assert line.startswith("data: ")
    assert '"job_id": "direct"' in line
    asyncio.run(resp.body_iterator.aclose())
