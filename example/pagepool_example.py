import argparse
import asyncio

from pagepool import PlaywrightPagePool, PoolConfig


async def main_async(cdp: str) -> None:
    config = PoolConfig(
        cdp_endpoints=[cdp],
        min_active_page=1,
        max_idle_pages=2,
    )
    async with PlaywrightPagePool(config) as pool:
        async with pool.page() as page:
            await page.goto("https://example.com", wait_until="domcontentloaded")
            title = await page.title()
            content = await page.content()
            print("title:", title)
            print("content_prefix:", content[:200])


def main() -> int:
    parser = argparse.ArgumentParser(description="Pagepool demo for example.com")
    parser.add_argument("--cdp", default="http://127.0.0.1:9222")
    args = parser.parse_args()
    asyncio.run(main_async(args.cdp))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
