import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse

from backend.core.events import ServerEvent, event_bus

logger = logging.getLogger("events")
router = APIRouter(prefix="/api/events", tags=["events"])


async def event_generator(request: Request, bus=event_bus) -> AsyncIterator[bytes]:
    """Streams server-sent events to connected HTTP clients with keepalive comments."""
    sub = bus.subscribe()
    try:
        # 1. Send initial connected event
        initial = {
            "status": "connected",
            "timestamp": time.time(),
            "subscribers": bus.subscriber_count,
        }
        yield f"event: connected\ndata: {json.dumps(initial)}\n\n".encode("utf-8")

        while True:
            try:
                # Wait for next event or 15s keepalive deadline
                event: ServerEvent = await asyncio.wait_for(
                    sub.get(),
                    timeout=15.0,
                )
                yield event.to_sse_bytes()
            except asyncio.TimeoutError:
                # Emit SSE comment to keep socket open across idle periods
                yield b": keepalive\n\n"
            except StopAsyncIteration:
                break
    except asyncio.CancelledError:
        pass
    finally:
        await sub.aclose()


@router.get("", response_class=StreamingResponse)
@router.get("/stream", response_class=StreamingResponse)
async def sse_stream(request: Request):
    """Single-connection Server-Sent Events endpoint for real-time search and download progress."""
    bus = getattr(request.app.state, "event_bus", event_bus)
    return StreamingResponse(
        event_generator(request, bus),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "Content-Type": "text/event-stream",
            "X-Accel-Buffering": "no",
        },
    )
