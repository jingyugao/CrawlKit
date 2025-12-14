"""Comprehensive test script for pwutil page pool."""

import asyncio
import sys
import time
from pathlib import Path

# Add parent directory to path to import pwutil
sys.path.insert(0, str(Path(__file__).parent.parent))

from pwutil import (
    PlaywrightPagePool,
    PoolConfig,
    round_robin_balancer,
    least_connections_balancer,
)


def print_header(text):
    """Print test section header."""
    print("\n" + "=" * 60)
    print(f"  {text}")
    print("=" * 60)


def print_success(text):
    """Print success message."""
    print(f"✓ {text}")


def print_info(text):
    """Print info message."""
    print(f"ℹ {text}")


def print_stats(pool):
    """Print pool statistics."""
    stats = pool.get_stats()
    health = pool.get_health()

    print("\n📊 Pool Statistics:")
    for endpoint, stat in stats.items():
        print(f"\n  {endpoint}:")
        print(f"    Healthy: {health[endpoint]}")
        print(f"    Circuit State: {stat.circuit_state}")
        print(f"    Total Pages: {stat.total_pages}")
        print(f"    Active Pages: {stat.active_pages}")
        print(f"    Active Contexts: {stat.active_contexts}")
        print(f"    Total Contexts: {stat.total_contexts}")
        print(f"    Load Score: {stat.load_score:.2f}")
        print(f"    Success/Error: {stat.success_count}/{stat.error_count}")


async def test_basic_usage():
    """Test 1: Basic pool usage."""
    print_header("Test 1: Basic Pool Usage")

    config = PoolConfig(
        cdp_endpoints=['http://localhost:9222']
    )

    async with PlaywrightPagePool(config) as pool:
        print_info("Pool started")

        # Test single page
        async with pool.page() as page:
            await page.goto('http://localhost:8080/')
            title = await page.title()
            print_success(f"Scraped home page: '{title}'")

        # Test multiple pages
        for i in range(1, 4):
            async with pool.page() as page:
                await page.goto(f'http://localhost:8080/page/{i}')
                title = await page.title()
                print_success(f"Scraped page {i}: '{title}'")

        print_stats(pool)

    print_success("Test 1 completed")


async def test_load_balancing():
    """Test 2: Load balancing across multiple endpoints."""
    print_header("Test 2: Load Balancing")

    config = PoolConfig(
        cdp_endpoints=[
            'http://localhost:9222',
            'http://localhost:9223',
        ],
        load_balancer=round_robin_balancer(),
    )

    async with PlaywrightPagePool(config) as pool:
        print_info("Pool started with 2 Chrome instances")

        # Scrape multiple pages concurrently
        async def scrape(page_num):
            async with pool.page() as page:
                await page.goto(f'http://localhost:8080/page/{page_num}')
                return await page.title()

        # Create 10 concurrent tasks
        tasks = [scrape(i) for i in range(1, 11)]
        results = await asyncio.gather(*tasks)

        print_success(f"Scraped {len(results)} pages concurrently")

        print_stats(pool)

    print_success("Test 2 completed")


async def test_high_concurrency():
    """Test 3: High concurrency scraping."""
    print_header("Test 3: High Concurrency")

    config = PoolConfig(
        cdp_endpoints=[
            'http://localhost:9222',
            'http://localhost:9223',
        ],
        max_connections_per_endpoint=2,
        max_contexts_per_connection=5,
        reuse_contexts=True,
        load_balancer=least_connections_balancer,
    )

    async with PlaywrightPagePool(config) as pool:
        print_info("Testing with 50 concurrent requests...")

        start_time = time.time()

        async def scrape(page_num):
            try:
                async with pool.page() as page:
                    await page.goto(f'http://localhost:8080/page/{page_num}', timeout=10000)
                    return await page.title()
            except Exception as e:
                return f"Error: {e}"

        # Create 50 concurrent tasks
        tasks = [scrape(i) for i in range(1, 51)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        elapsed = time.time() - start_time
        successful = sum(1 for r in results if isinstance(r, str) and not r.startswith("Error"))

        print_success(f"Completed {successful}/50 requests in {elapsed:.2f}s")
        print_info(f"Average: {elapsed/50:.3f}s per request")

        print_stats(pool)

    print_success("Test 3 completed")


async def test_ttl_management():
    """Test 4: TTL management."""
    print_header("Test 4: TTL Management")

    config = PoolConfig(
        cdp_endpoints=['http://localhost:9222'],
        page_ttl=5.0,       # 5 seconds
        context_ttl=15.0,   # 15 seconds
        idle_timeout=10.0,  # 10 seconds
        reuse_contexts=True,
    )

    async with PlaywrightPagePool(config) as pool:
        print_info("Testing page TTL (5s)...")

        # Acquire a page and check TTL
        page_wrapper = await pool.acquire_page()
        print_success(f"Page TTL expired: {page_wrapper.is_ttl_expired()}")

        # Wait and check again
        await asyncio.sleep(6)
        print_success(f"After 6s, Page TTL expired: {page_wrapper.is_ttl_expired()}")

        await page_wrapper.release()

        print_stats(pool)

    print_success("Test 4 completed")


async def test_dynamic_endpoints():
    """Test 5: Dynamic endpoint management."""
    print_header("Test 5: Dynamic Endpoints")

    # Simulated endpoint discovery
    class EndpointDiscovery:
        def __init__(self):
            self.endpoints = ['http://localhost:9222']

        def get_endpoints(self):
            return self.endpoints.copy()

        def add_endpoint(self, endpoint):
            if endpoint not in self.endpoints:
                self.endpoints.append(endpoint)

        def remove_endpoint(self, endpoint):
            if endpoint in self.endpoints:
                self.endpoints.remove(endpoint)

    discovery = EndpointDiscovery()

    config = PoolConfig(
        cdp_endpoints=discovery.get_endpoints,
        endpoints_refresh_interval=3.0,  # Refresh every 3 seconds
    )

    async with PlaywrightPagePool(config) as pool:
        print_info("Started with 1 endpoint")
        print_stats(pool)

        # Scrape with 1 endpoint
        async with pool.page() as page:
            await page.goto('http://localhost:8080/')
            print_success("Scraped with 1 endpoint")

        # Add second endpoint
        print_info("\nAdding second endpoint...")
        discovery.add_endpoint('http://localhost:9223')
        await asyncio.sleep(4)  # Wait for refresh

        print_stats(pool)

        # Scrape with 2 endpoints
        async with pool.page() as page:
            await page.goto('http://localhost:8080/')
            print_success("Scraped with 2 endpoints")

        # Remove second endpoint
        print_info("\nRemoving second endpoint...")
        discovery.remove_endpoint('http://localhost:9223')
        await asyncio.sleep(4)  # Wait for refresh

        print_stats(pool)

    print_success("Test 5 completed")


async def test_custom_context():
    """Test 6: Custom context factory."""
    print_header("Test 6: Custom Context Factory")

    async def mobile_context_factory(browser):
        """Create mobile context."""
        return await browser.new_context(
            viewport={'width': 375, 'height': 667},
            user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X)',
            is_mobile=True,
        )

    config = PoolConfig(
        cdp_endpoints=['http://localhost:9222'],
        context_factory=mobile_context_factory,
    )

    async with PlaywrightPagePool(config) as pool:
        print_info("Testing mobile context...")

        async with pool.page() as page:
            await page.goto('http://localhost:8080/')

            # Verify mobile viewport
            viewport = page.viewport_size
            print_success(f"Mobile viewport: {viewport}")

            title = await page.title()
            print_success(f"Scraped with mobile context: '{title}'")

        print_stats(pool)

    print_success("Test 6 completed")


async def test_error_handling():
    """Test 7: Error handling and circuit breaker."""
    print_header("Test 7: Error Handling")

    config = PoolConfig(
        cdp_endpoints=[
            'http://localhost:9222',
            'http://invalid:9999',  # Invalid endpoint
        ],
        failure_threshold=3,
        recovery_timeout=5.0,
    )

    async with PlaywrightPagePool(config) as pool:
        print_info("Testing with invalid endpoint...")

        # This should still work with the valid endpoint
        async with pool.page() as page:
            await page.goto('http://localhost:8080/')
            title = await page.title()
            print_success(f"Scraped despite invalid endpoint: '{title}'")

        print_stats(pool)

    print_success("Test 7 completed")


async def main():
    """Run all tests."""
    print("\n" + "🚀" * 30)
    print("  PWUTIL PAGE POOL - COMPREHENSIVE TESTS")
    print("🚀" * 30)

    tests = [
        ("Basic Usage", test_basic_usage),
        ("Load Balancing", test_load_balancing),
        ("High Concurrency", test_high_concurrency),
        ("TTL Management", test_ttl_management),
        ("Dynamic Endpoints", test_dynamic_endpoints),
        ("Custom Context", test_custom_context),
        ("Error Handling", test_error_handling),
    ]

    results = []
    for name, test_func in tests:
        try:
            await test_func()
            results.append((name, "PASSED"))
        except Exception as e:
            print(f"\n❌ Test failed: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, f"FAILED: {e}"))

    # Print summary
    print("\n" + "=" * 60)
    print("  TEST SUMMARY")
    print("=" * 60)

    for name, status in results:
        symbol = "✓" if status == "PASSED" else "❌"
        print(f"{symbol} {name}: {status}")

    passed = sum(1 for _, status in results if status == "PASSED")
    print(f"\nTotal: {passed}/{len(tests)} tests passed")


if __name__ == '__main__':
    asyncio.run(main())
