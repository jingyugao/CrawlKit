import argparse
import asyncio
import signal
from datetime import datetime, timezone

from playwright.async_api import async_playwright


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def watch_disconnect(cdp_endpoint: str) -> int:
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(cdp_endpoint)

        disconnected = asyncio.Event()

        def on_disconnect() -> None:
            print(f"[{utc_now()}] playwright disconnected from cdp")
            disconnected.set()

        browser.on("disconnected", on_disconnect)

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()
        await page.goto("about:blank")
        print(f"[{utc_now()}] connected to cdp: {cdp_endpoint}")

        await disconnected.wait()
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch Playwright disconnect on scale down")
    parser.add_argument(
        "--cdp",
        default="http://127.0.0.1:9222",
        help="CDP HTTP endpoint (e.g. http://127.0.0.1:9222)",
    )
    args = parser.parse_args()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    stop_event = asyncio.Event()

    def handle_signal(signum: int, _frame) -> None:
        print(f"[{utc_now()}] received signal {signum}, exiting")
        loop.call_soon_threadsafe(stop_event.set)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    async def runner() -> int:
        watcher = asyncio.create_task(watch_disconnect(args.cdp))
        stopper = asyncio.create_task(stop_event.wait())
        done, _ = await asyncio.wait(
            {watcher, stopper}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            if task is watcher:
                return task.result()
        return 0

    return loop.run_until_complete(runner())


if __name__ == "__main__":
    raise SystemExit(main())
