import argparse
import asyncio

from pagepool import PlaywrightPagePool, PoolConfig


async def run_scenario(pool: PlaywrightPagePool, name: str, url: str) -> None:
    async with pool.page() as page:
        await page.goto(url, wait_until="domcontentloaded")
        title = await page.title()
        content = await page.content()
        print(f"[{name}] title:", title)
        print(f"[{name}] content_prefix:", content[:200])


async def main_async(cdp: str) -> None:
    config = PoolConfig(
        cdp_endpoints=[cdp],
        min_active_page=3,
        max_idle_pages=3,
    )
    pool = PlaywrightPagePool(config)
    await pool.start()
    try:
        scenarios = [
            ("example", "https://example.com"),
            ("python", "https://www.python.org"),
            ("wikipedia", "https://www.wikipedia.org"),
        ]
        await asyncio.gather(*(run_scenario(pool, name, url) for name, url in scenarios))
    finally:
        await pool.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Pagepool demo for multi scenarios")
    parser.add_argument("--cdp", default="http://127.0.0.1:9222")
    args = parser.parse_args()
    asyncio.run(main_async(args.cdp))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
