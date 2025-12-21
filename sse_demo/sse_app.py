import asyncio
import json
import os
import signal
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

MAX_DURATION = int(os.getenv("SSE_MAX_DURATION", "180"))
DEFAULT_INTERVAL = float(os.getenv("SSE_INTERVAL", "1.0"))
SHUTDOWN_WAIT = int(os.getenv("SSE_SHUTDOWN_WAIT", str(MAX_DURATION + 10)))

shutdown_event = asyncio.Event()
active_lock = asyncio.Lock()
active_connections = 0


@asynccontextmanager
async def lifespan(_: FastAPI):
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)

    yield

    shutdown_event.set()
    deadline = time.time() + SHUTDOWN_WAIT
    while True:
        async with active_lock:
            remaining = active_connections
        if remaining == 0 or time.time() >= deadline:
            break
        await asyncio.sleep(0.2)


app = FastAPI(lifespan=lifespan)

async def _inc_active() -> None:
    global active_connections
    async with active_lock:
        active_connections += 1


async def _dec_active() -> None:
    global active_connections
    async with active_lock:
        active_connections = max(0, active_connections - 1)


@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/ready")
async def ready():
    if shutdown_event.is_set():
        return PlainTextResponse("shutting down", status_code=503)
    return {"ok": True}


@app.get("/sse")
async def sse(
    request: Request,
    duration: int = Query(MAX_DURATION, ge=1),
    interval: float = Query(DEFAULT_INTERVAL, ge=0.2),
):
    if shutdown_event.is_set():
        return PlainTextResponse("shutting down", status_code=503)

    duration = min(duration, MAX_DURATION)
    interval = max(interval, 0.2)
    start = time.time()

    hostname = os.getenv("HOSTNAME", "unknown")

    async def event_stream():
        seq = 0
        disconnected = False
        await _inc_active()
        try:
            while True:
                if await request.is_disconnected():
                    disconnected = True
                    break
                elapsed = time.time() - start
                if elapsed >= duration:
                    break
                payload = {"seq": seq, "elapsed": round(elapsed, 3), "hostname": hostname}
                data = json.dumps(payload)
                yield f"id: {seq}\nevent: tick\ndata: {data}\n\n"
                seq += 1
                await asyncio.sleep(interval)

            if not disconnected:
                done_payload = json.dumps(
                    {"elapsed": round(time.time() - start, 3), "hostname": hostname}
                )
                yield f"event: done\ndata: {done_payload}\n\n"
        except asyncio.CancelledError:
            return
        finally:
            await _dec_active()

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)
