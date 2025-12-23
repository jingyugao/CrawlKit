import asyncio
import json
import os
import signal
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse

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


@app.get("/demo-page", response_class=HTMLResponse)
async def demo_page():
    html = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Demo Page</title>
  <style>
    :root {
      --bg1: #0b0f1a;
      --bg2: #1a2238;
      --accent: #f5d76e;
      --accent-2: #ff7f66;
      --ink: #f6f4ef;
    }
    body {
      margin: 0;
      font-family: "Georgia", "Times New Roman", serif;
      color: var(--ink);
      background: radial-gradient(1200px 700px at 20% 10%, #22305a 0%, var(--bg1) 40%),
                  radial-gradient(900px 600px at 80% 80%, #2a3c6f 0%, var(--bg2) 45%);
      min-height: 100vh;
      display: grid;
      place-items: center;
    }
    .card {
      width: min(900px, 92vw);
      background: rgba(14, 18, 32, 0.85);
      border: 1px solid rgba(255, 255, 255, 0.1);
      border-radius: 24px;
      padding: 36px;
      box-shadow: 0 30px 80px rgba(0, 0, 0, 0.4);
      backdrop-filter: blur(10px);
    }
    .title {
      font-size: clamp(28px, 4vw, 40px);
      margin: 0 0 16px;
      letter-spacing: 0.6px;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 16px;
      margin-top: 18px;
    }
    .tile {
      background: linear-gradient(135deg, rgba(255, 255, 255, 0.06), rgba(255, 255, 255, 0.02));
      border-radius: 16px;
      padding: 14px 16px;
      border: 1px solid rgba(255, 255, 255, 0.08);
      min-height: 90px;
    }
    .tile h4 {
      margin: 0 0 8px;
      font-size: 14px;
      color: var(--accent);
      text-transform: uppercase;
      letter-spacing: 1px;
    }
    .bar {
      height: 6px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.08);
      overflow: hidden;
    }
    .bar span {
      display: block;
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, var(--accent), var(--accent-2));
      transition: width 600ms ease;
    }
    .status {
      margin-top: 18px;
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 14px;
      color: #b9c6e4;
    }
    .dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--accent-2);
      box-shadow: 0 0 12px var(--accent-2);
      animation: pulse 1.2s infinite ease-in-out;
    }
    @keyframes pulse {
      0%, 100% { transform: scale(0.9); opacity: 0.6; }
      50% { transform: scale(1.2); opacity: 1; }
    }
  </style>
</head>
<body>
  <div class="card">
    <h1 class="title">Signal Warmup Dashboard</h1>
    <p>This page runs JS tasks, updates DOM, and then redirects.</p>
    <div class="grid" id="grid"></div>
    <div class="status"><span class="dot"></span><span id="statusText">Initializing...</span></div>
  </div>
  <script>
    const grid = document.getElementById("grid");
    const statusText = document.getElementById("statusText");
    const tiles = [];
    for (let i = 0; i < 8; i++) {
      const tile = document.createElement("div");
      tile.className = "tile";
      tile.innerHTML = "<h4>channel " + (i + 1) + "</h4><div class=\\"bar\\"><span></span></div>";
      grid.appendChild(tile);
      tiles.push(tile.querySelector("span"));
    }
    let step = 0;
    const interval = setInterval(() => {
      step += 1;
      tiles.forEach((bar, idx) => {
        const val = Math.min(100, (step * 13 + idx * 7) % 101);
        bar.style.width = val + "%";
      });
      statusText.textContent = "Processing step " + step;
      if (step >= 12) {
        clearInterval(interval);
        statusText.textContent = "Redirecting to example.com...";
        setTimeout(() => {
          window.location.href = "https://example.com";
        }, 500);
      }
    }, 120);
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html)
