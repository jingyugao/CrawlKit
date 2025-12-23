"""High-concurrency scraping example."""

import asyncio
from typing import List, Dict, Any
from pwutil import PlaywrightPagePool, PoolConfig


async def scrape_url(pool: PlaywrightPagePool, url: str) -> Dict[str, Any]:
    """Scrape a single URL.

    Args:
        pool: The page pool.
        url: URL to scrape.

    Returns:
        Dictionary with scraping results.
    """
    async with pool.page() as page:
        try:
            await page.goto(url, wait_until='networkidle', timeout=30000)

            title = await page.title()
            content = await page.content()

            return {
                'url': url,
                'title': title,
                'content_length': len(content),
                'status': 'success'
            }
        except Exception as e:
            return {
                'url': url,
                'status': 'error',
                'error': str(e)
            }


async def main():
    """Main function demonstrating high-concurrency scraping."""
    # Configuration for high concurrency
    config = PoolConfig(
        cdp_endpoints=[
            'http://localhost:9222',
            'http://localhost:9223',
            'http://localhost:9224',
        ],
        max_connections_per_endpoint=3,
        max_contexts_per_connection=15,
        reuse_contexts=True,
        auto_cleanup=True,
        # TTL settings
        page_ttl=300.0,      # 5 minutes
        context_ttl=1800.0,  # 30 minutes
        idle_timeout=600.0,  # 10 minutes
    )

    async with PlaywrightPagePool(config) as pool:
        # Generate URLs to scrape
        urls = [f'https://example.com/page/{i}' for i in range(100)]

        # Process in batches to avoid overwhelming the pool
        batch_size = 20
        all_results = []

        for i in range(0, len(urls), batch_size):
            batch = urls[i:i + batch_size]

            print(f"\nProcessing batch {i // batch_size + 1} ({len(batch)} URLs)...")

            # Process batch concurrently
            tasks = [scrape_url(pool, url) for url in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Filter out successful results
            successful = [r for r in results if isinstance(r, dict) and r.get('status') == 'success']
            errors = [r for r in results if isinstance(r, dict) and r.get('status') == 'error']

            all_results.extend(successful)

            print(f"  Completed: {len(successful)} success, {len(errors)} errors")

            # Print pool statistics
            stats = pool.get_stats()
            for endpoint, stat in stats.items():
                print(f"  {endpoint}: {stat.active_pages} active pages, "
                      f"{stat.active_contexts} contexts, healthy={stat.is_healthy}")

        print(f"\n\nTotal results: {len(all_results)} successful scrapes out of {len(urls)}")


if __name__ == '__main__':
    asyncio.run(main())
