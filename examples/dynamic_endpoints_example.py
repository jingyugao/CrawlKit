"""Example demonstrating dynamic endpoint management."""

import asyncio
import random
from pagepool import PlaywrightPagePool, PoolConfig


# Simulated endpoint discovery (replace with real service discovery)
class ChromeEndpointDiscovery:
    """Simulates a service discovery system for Chrome endpoints."""

    def __init__(self):
        self.base_endpoints = [
            'http://localhost:9222',
            'http://localhost:9223',
        ]
        self.extra_endpoint = 'http://localhost:9224'
        self.include_extra = False

    def get_endpoints(self) -> list[str]:
        """Get current list of endpoints (simulating dynamic discovery)."""
        endpoints = self.base_endpoints.copy()

        # Simulate adding/removing endpoints dynamically
        if self.include_extra:
            endpoints.append(self.extra_endpoint)

        print(f"[Discovery] Returning {len(endpoints)} endpoints: {endpoints}")
        return endpoints

    def toggle_extra(self):
        """Toggle the extra endpoint (simulates scaling)."""
        self.include_extra = not self.include_extra
        status = "added" if self.include_extra else "removed"
        print(f"[Discovery] Extra endpoint {status}")


async def scrape_task(pool: PlaywrightPagePool, task_id: int):
    """Simulate a scraping task."""
    async with pool.page() as page:
        await page.goto('https://example.com')
        title = await page.title()
        print(f"[Task {task_id}] Scraped: {title}")
        await asyncio.sleep(2)  # Simulate work


async def main():
    """Main function demonstrating dynamic endpoint management."""
    # Create endpoint discovery
    discovery = ChromeEndpointDiscovery()

    # Configure pool with dynamic endpoints
    config = PoolConfig(
        cdp_endpoints=discovery.get_endpoints,  # Pass function, not list!
        endpoints_refresh_interval=10.0,  # Refresh every 10 seconds
        max_connections_per_endpoint=2,
        max_contexts_per_connection=5,
    )

    async with PlaywrightPagePool(config) as pool:
        print("Pool started with dynamic endpoints\n")

        # Run scraping tasks in background
        tasks = []

        for i in range(20):
            task = asyncio.create_task(scrape_task(pool, i))
            tasks.append(task)

            # Simulate endpoint scaling after 5 tasks
            if i == 5:
                print("\n[Scaling] Adding extra endpoint...\n")
                discovery.toggle_extra()
                await asyncio.sleep(11)  # Wait for refresh

            # Simulate endpoint scaling down after 15 tasks
            if i == 15:
                print("\n[Scaling] Removing extra endpoint...\n")
                discovery.toggle_extra()
                await asyncio.sleep(11)  # Wait for refresh

            await asyncio.sleep(1)

        # Wait for all tasks to complete
        await asyncio.gather(*tasks)

        # Print final stats
        print("\n=== Final Statistics ===")
        stats = pool.get_stats()
        for endpoint, stat in stats.items():
            print(f"\n{endpoint}:")
            print(f"  Total pages: {stat.total_pages}")
            print(f"  Active pages: {stat.active_pages}")
            print(f"  Active contexts: {stat.active_contexts}")
            print(f"  Healthy: {stat.is_healthy}")


async def main_with_async_discovery():
    """Example using async function for endpoint discovery."""

    async def get_endpoints_from_k8s() -> list[str]:
        """Simulate async endpoint discovery from Kubernetes."""
        # Simulate API call
        await asyncio.sleep(0.1)

        # In real scenario, this would call Kubernetes API
        return [
            'http://chrome-pod-1:9222',
            'http://chrome-pod-2:9222',
        ]

    config = PoolConfig(
        cdp_endpoints=get_endpoints_from_k8s,  # Async function
        endpoints_refresh_interval=30.0,
    )

    async with PlaywrightPagePool(config) as pool:
        async with pool.page() as page:
            await page.goto('https://example.com')
            print(f"Title: {await page.title()}")


if __name__ == '__main__':
    print("=== Dynamic Endpoints Example ===\n")
    asyncio.run(main())

    print("\n\n=== Async Discovery Example ===\n")
    asyncio.run(main_with_async_discovery())
