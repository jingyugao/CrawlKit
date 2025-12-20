import asyncio
import json
import os
import time
from fastapi import FastAPI, Query, Request
from fastapi.responses import StreamingResponse

app = FastAPI()

MAX_DURATION = int(os.getenv("SSE_MAX_DURATION", "180"))
DEFAULT_INTERVAL = float(os.getenv("SSE_INTERVAL", "1.0"))


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/sse")
async def sse(
    request: Request,
    duration: int = Query(MAX_DURATION, ge=1),
    interval: float = Query(DEFAULT_INTERVAL, ge=0.2),
):
    duration = min(duration, MAX_DURATION)
    interval = max(interval, 0.2)
    start = time.time()

    async def event_stream():
        seq = 0
        disconnected = False
        try:
            while True:
                if await request.is_disconnected():
                    disconnected = True
                    break
                elapsed = time.time() - start
                if elapsed >= duration:
                    break
                payload = {"seq": seq, "elapsed": round(elapsed, 3)}
                data = json.dumps(payload)
                yield f"id: {seq}\nevent: tick\ndata: {data}\n\n"
                seq += 1
                await asyncio.sleep(interval)

            if not disconnected:
                done_payload = json.dumps({"elapsed": round(time.time() - start, 3)})
                yield f"event: done\ndata: {done_payload}\n\n"
        except asyncio.CancelledError:
            return

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)
