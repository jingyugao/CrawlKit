"""Example usage of PagePool for managing Playwright pages."""
import asyncio
from playwright.async_api import async_playwright
from pagepool import PagePool


async def main():
    """Demonstrate PagePool usage with various features."""
    async with async_playwright() as p:
        # Connect to browser (or launch)
        # For CDP connection:
        # browser = await p.chromium.connect_over_cdp("ws://localhost:9222")
        # For local launch:
        browser = await p.chromium.launch()

        try:
            # Define context creation function
            async def create_context():
                return await browser.new_context(
                    viewport={'width': 1920, 'height': 1080},
                    user_agent='PagePool/Example',
                )

            # Create pool with configuration
            pool = PagePool(
                browser=browser,
                new_context_func=create_context,
                min_idle_pages=5,           # Keep 5 idle pages ready
                max_total_pages=50,          # Max 50 total pages
                max_idle_pages=10,           # Max 10 idle pages
                max_pages_per_context=10,    # Max 10 pages per context
                context_ttl=300.0,           # Context expires after 5 minutes
                max_page_uses=50,            # Page expires after 50 uses
            )

            # Start the pool (pre-creates min_idle_pages)
            await pool.start()

            try:
                # Example 1: Using context manager (recommended)
                print("Example 1: Context manager usage")
                async with pool.page() as page:
                    await page.goto('https://example.com')
                    title = await page.title()
                    print(f"Page title: {title}")

                # Example 2: Manual acquire/release
                print("\nExample 2: Manual acquire/release")
                page_wrapper = await pool.get_page()
                try:
                    await page_wrapper.obj.goto('https://example.com')
                    print(f"Page URL: {page_wrapper.obj.url}")
                finally:
                    await pool.release_page(page_wrapper)

                # Example 3: Concurrent page usage
                print("\nExample 3: Concurrent usage (10 pages)")

                async def fetch_url(url: str):
                    async with pool.page() as page:
                        await page.goto(url)
                        return await page.title()

                urls = [
                    'https://example.com',
                    'https://www.iana.org',
                ] * 5  # 10 URLs total

                titles = await asyncio.gather(*[fetch_url(url) for url in urls])
                print(f"Fetched {len(titles)} pages concurrently")

                # Example 4: Check pool stats
                print("\nExample 4: Pool statistics")
                stats = pool.get_stats()
                print(f"Stats: {stats}")

                # Example 5: Custom health check
                print("\nExample 5: Custom health check")

                async def custom_health_check(page_wrapper):
                    """Custom health check: verify page can execute JS."""
                    try:
                        result = await asyncio.wait_for(
                            page_wrapper.obj.evaluate("1 + 1"),
                            timeout=1.0
                        )
                        return result == 2
                    except:
                        return False

                pool_with_custom_check = PagePool(
                    browser=browser,
                    new_context_func=create_context,
                    min_idle_pages=2,
                    max_total_pages=10,
                    health_check=custom_health_check,
                )

                await pool_with_custom_check.start()
                try:
                    async with pool_with_custom_check.page() as page:
                        await page.goto('https://example.com')
                        print("Custom health check passed!")
                finally:
                    await pool_with_custom_check.stop()

            finally:
                # Stop the pool (closes all contexts and pages)
                await pool.stop()

        finally:
            await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
