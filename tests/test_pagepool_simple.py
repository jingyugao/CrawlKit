"""Simplified PagePool tests that can run without full Playwright setup.

This is a demo to show the test structure. For full integration tests,
see test_pagepool.py (requires Playwright system dependencies).
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from pagepool import PagePool, PoolNotStartedError
from pagepool.wrappers import ContextWrapper, PageWrapper


class MockBrowser:
    """Mock Playwright Browser."""

    def __init__(self):
        self._connected = True

    def is_connected(self):
        return self._connected

    async def new_context(self, **kwargs):
        """Create mock context."""
        ctx = MockContext()
        return ctx


class MockContext:
    """Mock BrowserContext."""

    def __init__(self):
        self._closed = False
        self._pages = []

    async def new_page(self):
        """Create mock page."""
        page = MockPage()
        self._pages.append(page)
        return page

    async def close(self):
        """Close context."""
        self._closed = True
        for page in self._pages:
            await page.close()


class MockPage:
    """Mock Page."""

    def __init__(self):
        self._closed = False
        self._url = 'about:blank'
        self.title_value = 'Mock Page'

    def is_closed(self):
        return self._closed

    async def goto(self, url, **kwargs):
        """Navigate to URL."""
        if self._closed:
            raise Exception("Page closed")
        self._url = url

    async def title(self):
        """Get title."""
        return self.title_value

    async def evaluate(self, script):
        """Execute JS."""
        if script == "1 + 1":
            return 2
        return None

    async def close(self):
        """Close page."""
        self._closed = True

    @property
    def url(self):
        return self._url


class TestWrappers:
    """Test wrapper classes."""

    def test_context_wrapper_inc_dec(self):
        """Test ContextWrapper inc/dec pages."""
        ctx = ContextWrapper(
            obj=MagicMock(),
            created_at=datetime.now(),
        )

        assert ctx.active_pages == 0

        ctx.inc_pages()
        assert ctx.active_pages == 1

        ctx.inc_pages()
        assert ctx.active_pages == 2

        ctx.dec_pages()
        assert ctx.active_pages == 1

    def test_context_wrapper_ttl_expired(self):
        """Test ContextWrapper TTL checking."""
        import time

        ctx = ContextWrapper(
            obj=MagicMock(),
            created_at=datetime.now(),
        )

        # No TTL - never expires
        assert ctx.ttl_expired(None) is False

        # TTL 1 second - not expired yet
        assert ctx.ttl_expired(1.0) is False

        # Wait and check again
        time.sleep(1.1)
        assert ctx.ttl_expired(1.0) is True

    def test_page_wrapper_usage_count(self):
        """Test PageWrapper usage counting."""
        ctx_mock = MagicMock()
        page_wrapper = PageWrapper(
            obj=MagicMock(),
            context=ctx_mock,
            created_at=datetime.now(),
        )

        assert page_wrapper.use_count == 0

        page_wrapper.increment_use()
        assert page_wrapper.use_count == 1

        page_wrapper.increment_use()
        assert page_wrapper.use_count == 2

        # Check max uses
        assert page_wrapper.is_max_uses_exceeded(None) is False
        assert page_wrapper.is_max_uses_exceeded(3) is False
        assert page_wrapper.is_max_uses_exceeded(2) is True
        assert page_wrapper.is_max_uses_exceeded(1) is True


class TestBasicMockFunctionality:
    """Test basic PagePool operations with mocks."""

    @pytest.mark.asyncio
    async def test_pool_lifecycle(self):
        """Test pool start/stop."""
        browser = MockBrowser()

        async def create_context():
            return await browser.new_context()

        pool = PagePool(
            browser=browser,
            new_context_func=create_context,
            min_idle_pages=2,
            max_total_pages=10,
        )

        # Before start
        assert pool._started is False

        # Start
        await pool.start()
        assert pool._started is True
        assert pool.idle_pages.qsize() == 2  # Warmup created 2 pages

        # Stop
        await pool.stop()
        assert pool._started is False
        assert pool.idle_pages.qsize() == 0

    @pytest.mark.asyncio
    async def test_pool_not_started_error(self):
        """Test that operations fail when pool not started."""
        browser = MockBrowser()

        pool = PagePool(
            browser=browser,
            new_context_func=lambda: browser.new_context(),
            min_idle_pages=1,
        )

        # Should raise error
        with pytest.raises(PoolNotStartedError):
            await pool.get_page()

    @pytest.mark.asyncio
    async def test_get_and_release_page(self):
        """Test basic get/release flow."""
        browser = MockBrowser()

        pool = PagePool(
            browser=browser,
            new_context_func=lambda: browser.new_context(),
            min_idle_pages=2,
            max_total_pages=10,
        )

        await pool.start()

        try:
            # Get page
            page_wrapper = await pool.get_page()
            assert page_wrapper is not None
            assert not page_wrapper.obj.is_closed()
            assert page_wrapper.use_count == 1

            stats = pool.get_stats()
            assert stats['active_pages'] == 1

            # Release page
            await pool.release_page(page_wrapper)

            await asyncio.sleep(0.1)
            stats = pool.get_stats()
            assert stats['active_pages'] == 0
        finally:
            await pool.stop()

    @pytest.mark.asyncio
    async def test_context_manager(self):
        """Test pool.page() context manager."""
        browser = MockBrowser()

        pool = PagePool(
            browser=browser,
            new_context_func=lambda: browser.new_context(),
            min_idle_pages=1,
        )

        await pool.start()

        try:
            async with pool.page() as page:
                assert not page.is_closed()
                await page.goto('about:blank')

            # Page should be released
            stats = pool.get_stats()
            assert stats['active_pages'] == 0
        finally:
            await pool.stop()

    @pytest.mark.asyncio
    async def test_concurrent_usage(self):
        """Test concurrent page usage."""
        browser = MockBrowser()

        pool = PagePool(
            browser=browser,
            new_context_func=lambda: browser.new_context(),
            min_idle_pages=3,
            max_total_pages=10,
        )

        await pool.start()

        try:
            async def use_page(n):
                async with pool.page() as page:
                    await page.goto(f'data:text/html,<h1>Page {n}</h1>')
                    await asyncio.sleep(0.01)
                    return await page.title()

            # Use 5 pages concurrently
            results = await asyncio.gather(*[use_page(i) for i in range(5)])

            assert len(results) == 5

            # All pages should be released
            await asyncio.sleep(0.1)
            stats = pool.get_stats()
            assert stats['active_pages'] == 0
        finally:
            await pool.stop()

    @pytest.mark.asyncio
    async def test_max_total_pages_limit(self):
        """Test that total pages respects limit."""
        browser = MockBrowser()

        pool = PagePool(
            browser=browser,
            new_context_func=lambda: browser.new_context(),
            min_idle_pages=2,
            max_total_pages=5,
        )

        await pool.start()

        try:
            # Get 4 pages
            pages = []
            for _ in range(4):
                pages.append(await pool.get_page())

            stats = pool.get_stats()
            assert stats['total_pages'] <= 5

            # Release one - should be destroyed (not returned)
            await pool.release_page(pages[0])
            await asyncio.sleep(0.1)

            stats = pool.get_stats()
            assert stats['total_pages'] <= 5

            # Release all
            for page in pages[1:]:
                await pool.release_page(page)
        finally:
            await pool.stop()

    @pytest.mark.asyncio
    async def test_page_usage_limit(self):
        """Test page destroyed after max uses."""
        browser = MockBrowser()

        pool = PagePool(
            browser=browser,
            new_context_func=lambda: browser.new_context(),
            min_idle_pages=1,
            max_total_pages=10,
            max_page_uses=2,  # Low limit for testing
        )

        await pool.start()

        try:
            # Use page 2 times
            for i in range(2):
                page_wrapper = await pool.get_page()
                assert page_wrapper.use_count == i + 1

                if i < 1:
                    await pool.release_page(page_wrapper)
                    await asyncio.sleep(0.1)

            # Last release should destroy it
            first_page_id = id(page_wrapper.obj)
            await pool.release_page(page_wrapper)
            await asyncio.sleep(0.1)

            # Get new page - should be different
            new_page = await pool.get_page()
            assert id(new_page.obj) != first_page_id

            await pool.release_page(new_page)
        finally:
            await pool.stop()

    @pytest.mark.asyncio
    async def test_get_stats(self):
        """Test get_stats() returns correct info."""
        browser = MockBrowser()

        pool = PagePool(
            browser=browser,
            new_context_func=lambda: browser.new_context(),
            min_idle_pages=3,
            max_total_pages=10,
        )

        await pool.start()

        try:
            stats = pool.get_stats()

            assert 'total_contexts' in stats
            assert 'idle_pages' in stats
            assert 'active_pages' in stats
            assert 'total_pages' in stats
            assert 'started' in stats

            assert stats['started'] is True
            assert stats['total_contexts'] >= 1
            assert stats['idle_pages'] == 3
            assert stats['total_pages'] == stats['idle_pages'] + stats['active_pages']
        finally:
            await pool.stop()


if __name__ == '__main__':
    # Run a simple demo
    async def demo():
        print("=" * 60)
        print("PagePool Simple Demo")
        print("=" * 60)

        browser = MockBrowser()

        async def create_context():
            print("  → Creating new context")
            return await browser.new_context()

        pool = PagePool(
            browser=browser,
            new_context_func=create_context,
            min_idle_pages=3,
            max_total_pages=10,
            max_page_uses=5,
        )

        print("\n1. Starting pool...")
        await pool.start()
        print(f"   Stats: {pool.get_stats()}")

        print("\n2. Getting a page...")
        async with pool.page() as page:
            await page.goto('https://example.com')
            print(f"   Page URL: {page.url}")
            print(f"   Stats: {pool.get_stats()}")

        print("\n3. After releasing page...")
        await asyncio.sleep(0.1)
        print(f"   Stats: {pool.get_stats()}")

        print("\n4. Concurrent usage (5 pages)...")
        async def fetch(n):
            async with pool.page() as page:
                await page.goto(f'https://example{n}.com')
                return page.url

        results = await asyncio.gather(*[fetch(i) for i in range(5)])
        print(f"   Fetched: {results}")
        print(f"   Stats: {pool.get_stats()}")

        print("\n5. Stopping pool...")
        await pool.stop()
        print(f"   Stats: {pool.get_stats()}")

        print("\n" + "=" * 60)
        print("Demo completed successfully!")
        print("=" * 60)

    asyncio.run(demo())
