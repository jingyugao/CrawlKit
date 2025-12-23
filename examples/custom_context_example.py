"""Example demonstrating custom context factories and load balancers."""

import asyncio
from playwright.async_api import Browser, BrowserContext
from pwutil import PlaywrightPagePool, PoolConfig, ConnectionStats, least_connections_balancer


# Custom context factory for mobile scraping
async def mobile_context_factory(browser: Browser) -> BrowserContext:
    """Create a context configured for mobile devices."""
    return await browser.new_context(
        viewport={'width': 375, 'height': 667},
        user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15',
        device_scale_factor=2,
        is_mobile=True,
        has_touch=True,
        locale='en-US',
        timezone_id='America/Los_Angeles',
    )


# Custom context factory for desktop scraping with stealth
async def desktop_stealth_context_factory(browser: Browser) -> BrowserContext:
    """Create a context configured for stealthy desktop scraping."""
    return await browser.new_context(
        viewport={'width': 1920, 'height': 1080},
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        locale='en-US',
        timezone_id='America/New_York',
        ignore_https_errors=True,
        extra_http_headers={
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        }
    )


# Custom load balancer
def custom_load_balancer(stats: dict[str, ConnectionStats]) -> str:
    """Select endpoint with least active pages and low error rate."""
    healthy = {
        ep: s for ep, s in stats.items()
        if s.is_healthy and s.circuit_state != "open"
    }

    if not healthy:
        raise Exception("No healthy endpoints")

    # Prefer endpoints with fewer errors and lower active pages
    def score(stat: ConnectionStats) -> float:
        error_penalty = stat.error_count * 10
        return stat.active_pages + error_penalty

    selected = min(healthy.items(), key=lambda x: score(x[1]))
    return selected[0]


async def mobile_scraping_example():
    """Example: Mobile scraping with custom context."""
    print("=== Mobile Scraping Example ===")

    config = PoolConfig(
        cdp_endpoints=['http://localhost:9222'],
        context_factory=mobile_context_factory,
        max_contexts_per_connection=5,
    )

    async with PlaywrightPagePool(config) as pool:
        async with pool.page() as page:
            await page.goto('https://example.com')

            # Verify mobile viewport
            viewport = page.viewport_size
            print(f"Viewport: {viewport}")

            title = await page.title()
            print(f"Title: {title}")


async def desktop_scraping_example():
    """Example: Desktop scraping with stealth context."""
    print("\n=== Desktop Stealth Scraping Example ===")

    config = PoolConfig(
        cdp_endpoints=['http://localhost:9222'],
        context_factory=desktop_stealth_context_factory,
        max_contexts_per_connection=5,
    )

    async with PlaywrightPagePool(config) as pool:
        async with pool.page() as page:
            await page.goto('https://example.com')

            # Verify desktop viewport
            viewport = page.viewport_size
            print(f"Viewport: {viewport}")

            title = await page.title()
            print(f"Title: {title}")


async def custom_load_balancer_example():
    """Example: Using custom load balancer."""
    print("\n=== Custom Load Balancer Example ===")

    config = PoolConfig(
        cdp_endpoints=[
            'http://localhost:9222',
            'http://localhost:9223',
        ],
        load_balancer=custom_load_balancer,
    )

    async with PlaywrightPagePool(config) as pool:
        # Scrape multiple URLs
        urls = ['https://example.com'] * 10

        async def scrape(url):
            async with pool.page() as page:
                await page.goto(url)
                return await page.title()

        results = await asyncio.gather(*[scrape(url) for url in urls])

        print(f"Scraped {len(results)} URLs")

        # Show load balancer stats
        stats = pool.get_stats()
        for endpoint, stat in stats.items():
            print(f"\n{endpoint}:")
            print(f"  Total pages: {stat.total_pages}")
            print(f"  Error count: {stat.error_count}")
            print(f"  Load score: {stat.load_score}")


async def builtin_load_balancer_example():
    """Example: Using built-in load balancer."""
    print("\n=== Built-in Load Balancer Example ===")

    config = PoolConfig(
        cdp_endpoints=[
            'http://localhost:9222',
            'http://localhost:9223',
        ],
        load_balancer=least_connections_balancer,  # Built-in strategy
    )

    async with PlaywrightPagePool(config) as pool:
        async with pool.page() as page:
            await page.goto('https://example.com')
            print(f"Title: {await page.title()}")


async def main():
    """Run all examples."""
    await mobile_scraping_example()
    await desktop_scraping_example()
    await custom_load_balancer_example()
    await builtin_load_balancer_example()


if __name__ == '__main__':
    asyncio.run(main())
