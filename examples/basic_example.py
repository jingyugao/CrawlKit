"""Basic example of using the Playwright page pool."""

import asyncio
from pwutil import PlaywrightPagePool, PoolConfig


async def main():
    """Simple example showing basic pool usage."""
    # Configure the pool
    config = PoolConfig(
        cdp_endpoints=['http://localhost:9222']
    )

    # Create and start the pool
    pool = PlaywrightPagePool(config)
    await pool.start()

    try:
        # Acquire a page
        page_wrapper = await pool.acquire_page()

        try:
            # Use the page
            await page_wrapper.page.goto('https://example.com')
            title = await page_wrapper.page.title()
            print(f"Title: {title}")

        finally:
            # Always release the page
            await page_wrapper.release()

    finally:
        # Cleanup
        await pool.stop()


async def main_with_context_manager():
    """Example using context managers (recommended)."""
    config = PoolConfig(
        cdp_endpoints=['http://localhost:9222']
    )

    # Use pool as context manager
    async with PlaywrightPagePool(config) as pool:
        # Use page as context manager
        async with pool.page() as page:
            await page.goto('https://example.com')
            title = await page.title()
            print(f"Title: {title}")


if __name__ == '__main__':
    print("Running basic example...")
    asyncio.run(main())

    print("\nRunning example with context managers...")
    asyncio.run(main_with_context_manager())
