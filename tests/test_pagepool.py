"""Comprehensive tests for PagePool."""

import asyncio

import pytest
from playwright.async_api import Browser, async_playwright

from pagepool import PagePool, PoolNotStartedError
from pagepool.wrappers import PageWrapper


@pytest.fixture
async def browser():
    """Fixture to provide a Playwright browser instance."""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        yield browser
        await browser.close()


@pytest.fixture
async def simple_pool(browser: Browser):
    """Fixture for a simple PagePool instance."""

    async def create_context():
        return await browser.new_context()

    pool = PagePool(
        browser=browser,
        new_context_func=create_context,
        min_idle_pages=3,
        max_total_pages=20,
    )
    await pool.start()
    yield pool
    await pool.stop()


class TestBasicFunctionality:
    """Test basic PagePool operations."""

    @pytest.mark.asyncio
    async def test_pool_start_creates_idle_pages(self, browser: Browser):
        """Test that start() creates min_idle_pages during warmup."""

        async def create_context():
            return await browser.new_context()

        pool = PagePool(
            browser=browser,
            new_context_func=create_context,
            min_idle_pages=5,
            max_total_pages=20,
        )

        await pool.start()

        try:
            stats = pool.get_stats()
            assert stats["idle_pages"] == 5, "Should have 5 idle pages after start"
            assert stats["total_contexts"] >= 1, "Should have at least 1 context"
            assert stats["started"] is True, "Pool should be started"
        finally:
            await pool.stop()

    @pytest.mark.asyncio
    async def test_get_and_release_page(self, simple_pool: PagePool):
        """Test basic get_page and release_page flow."""
        # Get a page
        page_wrapper = await simple_pool.get_page()
        assert page_wrapper is not None
        assert not page_wrapper.obj.is_closed()
        assert page_wrapper.use_count == 1

        # Use the page
        await page_wrapper.obj.goto("about:blank")

        # Release the page
        await simple_pool.release_page(page_wrapper)
        assert page_wrapper.released is True

        # Verify page returned to idle queue
        stats = simple_pool.get_stats()
        assert stats["active_pages"] == 0

    @pytest.mark.asyncio
    async def test_context_manager_usage(self, simple_pool: PagePool):
        """Test using pool.page() context manager."""
        async with simple_pool.page() as page:
            assert not page.is_closed()
            await page.goto("about:blank")
            title = await page.title()
            assert title == "", "about:blank should have empty title"

        # Page should be released after context manager exit
        stats = simple_pool.get_stats()
        assert stats["active_pages"] == 0

    @pytest.mark.asyncio
    async def test_page_cleared_on_release(self, simple_pool: PagePool):
        """Test that pages are navigated to about:blank on release."""

        async with simple_pool.page() as page:
            # Navigate to a real page
            await page.goto("data:text/html,<h1>Test</h1>")
            content = await page.content()
            assert "Test" in content

        # After release, get the same page and verify it's cleared
        await asyncio.sleep(0.1)  # Give time for release
        page_wrapper = await simple_pool.get_page()
        url = page_wrapper.obj.url
        await simple_pool.release_page(page_wrapper)

        assert url == "about:blank", "Page should be cleared to about:blank"


class TestHealthCheck:
    """Test health checking functionality."""

    @pytest.mark.asyncio
    async def test_default_health_check(self, browser: Browser):
        """Test default health check (browser.is_connected)."""

        async def create_context():
            return await browser.new_context()

        pool = PagePool(
            browser=browser,
            new_context_func=create_context,
            min_idle_pages=2,
            max_total_pages=10,
        )

        await pool.start()

        try:
            page_wrapper = await pool.get_page()
            is_healthy = await pool._check_page_health(page_wrapper)
            assert is_healthy is True
            await pool.release_page(page_wrapper)
        finally:
            await pool.stop()

    @pytest.mark.asyncio
    async def test_custom_health_check(self, browser: Browser):
        """Test custom health check function."""
        check_called = {"count": 0}

        async def custom_health_check(page_wrapper: PageWrapper) -> bool:
            check_called["count"] += 1
            try:
                # Verify page can execute JS
                result = await asyncio.wait_for(page_wrapper.obj.evaluate("1 + 1"), timeout=1.0)
                return result == 2
            except Exception:
                return False

        async def create_context():
            return await browser.new_context()

        pool = PagePool(
            browser=browser,
            new_context_func=create_context,
            min_idle_pages=2,
            max_total_pages=10,
            health_check=custom_health_check,
        )

        await pool.start()

        try:
            page_wrapper = await pool.get_page()
            assert check_called["count"] >= 1, "Custom health check should be called"
            await pool.release_page(page_wrapper)
        finally:
            await pool.stop()

    @pytest.mark.asyncio
    async def test_unhealthy_page_retry(self, browser: Browser):
        """Test that unhealthy pages trigger retry logic."""

        async def create_context():
            return await browser.new_context()

        pool = PagePool(
            browser=browser,
            new_context_func=create_context,
            min_idle_pages=3,
            max_total_pages=20,
        )

        await pool.start()

        try:
            # Get a page and close it (make it unhealthy)
            page_wrapper = await pool.get_page()
            await page_wrapper.obj.close()
            # Put the closed page back into idle queue manually
            await pool.idle_pages.put(page_wrapper)

            # Get page should retry and create a new one
            new_page_wrapper = await pool.get_page()
            assert not new_page_wrapper.obj.is_closed()
            await pool.release_page(new_page_wrapper)
        finally:
            await pool.stop()


class TestIdlePageManagement:
    """Test idle page queue management and refill."""

    @pytest.mark.asyncio
    async def test_background_refill_triggered(self, browser: Browser):
        """Test that background refill is triggered when idle < min."""

        async def create_context():
            return await browser.new_context()

        pool = PagePool(
            browser=browser,
            new_context_func=create_context,
            min_idle_pages=5,
            max_total_pages=20,
        )

        await pool.start()

        try:
            initial_idle = pool.idle_pages.qsize()
            assert initial_idle == 5

            # Acquire 3 pages (reduces idle to 2)
            pages = []
            for _ in range(3):
                pages.append(await pool.get_page())

            # Wait for background refill
            await asyncio.sleep(0.5)

            # Check that refill happened
            idle_after = pool.idle_pages.qsize()
            assert idle_after >= 3, "Background refill should have created pages"

            # Release pages
            for page in pages:
                await pool.release_page(page)
        finally:
            await pool.stop()

    @pytest.mark.asyncio
    async def test_refill_respects_total_limit(self, browser: Browser):
        """Test that refill respects max_total_pages limit."""

        async def create_context():
            return await browser.new_context()

        pool = PagePool(
            browser=browser,
            new_context_func=create_context,
            min_idle_pages=5,
            max_total_pages=8,  # Low limit
        )

        await pool.start()

        try:
            # Acquire 6 pages (idle=2, active=6, total=8)
            pages = []
            for _ in range(6):
                pages.append(await pool.get_page())

            # Wait for potential refill
            await asyncio.sleep(0.5)

            # Check that total doesn't exceed limit
            stats = pool.get_stats()
            assert stats["total_pages"] <= 8, "Total should not exceed max_total_pages"

            # Release pages
            for page in pages:
                await pool.release_page(page)
        finally:
            await pool.stop()


class TestTotalPagesLimit:
    """Test total pages limit enforcement."""

    @pytest.mark.asyncio
    async def test_release_destroys_when_total_at_limit(self, browser: Browser):
        """Test that pages are destroyed when total >= max_total_pages."""

        async def create_context():
            return await browser.new_context()

        pool = PagePool(
            browser=browser,
            new_context_func=create_context,
            min_idle_pages=3,
            max_total_pages=8,
            max_idle_pages=5,
        )

        await pool.start()

        try:
            # Acquire 6 pages
            pages = []
            for _ in range(6):
                pages.append(await pool.get_page())

            stats_before = pool.get_stats()
            assert stats_before["active_pages"] == 6

            # Release 1 page - should be destroyed (not returned) if at limit
            await pool.release_page(pages[0])

            await asyncio.sleep(0.1)
            stats_after = pool.get_stats()

            # Total should not exceed limit
            assert stats_after["total_pages"] <= 8

            # Release remaining
            for page in pages[1:]:
                await pool.release_page(page)
        finally:
            await pool.stop()

    @pytest.mark.asyncio
    async def test_max_idle_pages_limit(self, browser: Browser):
        """Test that pages are destroyed when idle >= max_idle_pages."""

        async def create_context():
            return await browser.new_context()

        pool = PagePool(
            browser=browser,
            new_context_func=create_context,
            min_idle_pages=2,
            max_total_pages=50,
            max_idle_pages=5,  # Set explicit idle limit
        )

        await pool.start()

        try:
            # Create many pages
            pages = []
            for _ in range(10):
                pages.append(await pool.get_page())

            # Release all (should not exceed max_idle_pages)
            for page in pages:
                await pool.release_page(page)

            await asyncio.sleep(0.2)

            stats = pool.get_stats()
            assert stats["idle_pages"] <= 5, "Idle pages should respect max_idle_pages"
        finally:
            await pool.stop()


class TestPageUsageLimit:
    """Test page usage limit enforcement."""

    @pytest.mark.asyncio
    async def test_page_destroyed_after_max_uses(self, browser: Browser):
        """Test that pages are destroyed after max_page_uses."""

        async def create_context():
            return await browser.new_context()

        pool = PagePool(
            browser=browser,
            new_context_func=create_context,
            min_idle_pages=2,
            max_total_pages=10,
            max_page_uses=3,  # Low limit for testing
        )

        await pool.start()

        try:
            # Get a page and use it 3 times
            for i in range(3):
                page_wrapper = await pool.get_page()
                assert page_wrapper.use_count == i + 1

                if i < 2:
                    # Release it (should return to pool)
                    await pool.release_page(page_wrapper)
                    await asyncio.sleep(0.1)

            # On 3rd release, should be destroyed
            page_id = id(page_wrapper.obj)
            await pool.release_page(page_wrapper)
            await asyncio.sleep(0.1)

            # Get a new page - should be different
            new_page_wrapper = await pool.get_page()
            assert id(new_page_wrapper.obj) != page_id, "Should get a new page"
            assert new_page_wrapper.use_count == 1

            await pool.release_page(new_page_wrapper)
        finally:
            await pool.stop()


class TestContextTTL:
    """Test context TTL rotation."""

    @pytest.mark.asyncio
    async def test_context_ttl_rotation(self, browser: Browser):
        """Test that contexts are rotated when approaching TTL."""

        async def create_context():
            return await browser.new_context()

        pool = PagePool(
            browser=browser,
            new_context_func=create_context,
            min_idle_pages=2,
            max_total_pages=10,
            context_ttl=2.0,  # Very short TTL for testing (2 seconds)
        )

        await pool.start()

        try:
            initial_contexts = len(pool.contexts)
            assert initial_contexts >= 1

            # Wait for context to approach TTL (lead time is 10% or 5s, whichever smaller)
            # For 2s TTL, lead time is 0.2s, so rotation happens at ~1.8s
            await asyncio.sleep(2.0)

            # Trigger rotation check by getting a page
            page_wrapper = await pool.get_page()

            # Should have created a new context
            assert len(pool.contexts) >= initial_contexts, "New context should be created"

            # Original context should be draining
            draining_count = sum(1 for ctx in pool.contexts if ctx.draining)
            assert draining_count >= 1, "At least one context should be draining"

            await pool.release_page(page_wrapper)
        finally:
            await pool.stop()

    @pytest.mark.asyncio
    async def test_draining_context_destroyed_after_pages_released(self, browser: Browser):
        """Test that draining contexts are destroyed when all pages released."""

        async def create_context():
            return await browser.new_context()

        pool = PagePool(
            browser=browser,
            new_context_func=create_context,
            min_idle_pages=1,
            max_total_pages=10,
            context_ttl=1.5,
        )

        await pool.start()

        try:
            # Get a page from initial context
            page1 = await pool.get_page()
            initial_ctx = page1.context

            # Wait for TTL
            await asyncio.sleep(1.6)

            # Get another page (triggers rotation)
            page2 = await pool.get_page()

            assert initial_ctx.draining is True, "Initial context should be draining"

            # Release page1 (last page from draining context)
            await pool.release_page(page1)
            await asyncio.sleep(0.1)

            # Draining context should be destroyed
            assert initial_ctx not in pool.contexts, "Draining context should be destroyed"

            await pool.release_page(page2)
        finally:
            await pool.stop()


class TestMaxPagesPerContext:
    """Test max_pages_per_context limit."""

    @pytest.mark.asyncio
    async def test_creates_new_context_when_limit_reached(self, browser: Browser):
        """Test that new context is created when max_pages_per_context is reached."""

        async def create_context():
            return await browser.new_context()

        pool = PagePool(
            browser=browser,
            new_context_func=create_context,
            min_idle_pages=1,
            max_total_pages=20,
            max_pages_per_context=3,  # Small limit
        )

        await pool.start()

        try:
            # Get 4 pages (should create 2 contexts)
            pages = []
            for _ in range(4):
                pages.append(await pool.get_page())

            assert len(pool.contexts) >= 2, "Should have created multiple contexts"

            # Release pages
            for page in pages:
                await pool.release_page(page)
        finally:
            await pool.stop()


class TestLifecycle:
    """Test pool lifecycle management."""

    @pytest.mark.asyncio
    async def test_pool_not_started_error(self, browser: Browser):
        """Test that operations fail when pool not started."""

        async def create_context():
            return await browser.new_context()

        pool = PagePool(
            browser=browser,
            new_context_func=create_context,
        )

        # Should raise error when getting page without starting
        with pytest.raises(PoolNotStartedError):
            await pool.get_page()

    @pytest.mark.asyncio
    async def test_stop_closes_all_contexts(self, browser: Browser):
        """Test that stop() closes all contexts and pages."""

        async def create_context():
            return await browser.new_context()

        pool = PagePool(
            browser=browser,
            new_context_func=create_context,
            min_idle_pages=3,
            max_total_pages=10,
        )

        await pool.start()

        # Get some pages
        pages = []
        for _ in range(2):
            pages.append(await pool.get_page())

        # Stop pool
        await pool.stop()

        # All contexts should be closed
        assert len(pool.contexts) == 0
        assert pool.idle_pages.qsize() == 0
        assert pool._started is False

    @pytest.mark.asyncio
    async def test_context_manager_lifecycle(self, browser: Browser):
        """Test pool context manager (__aenter__/__aexit__)."""

        async def create_context():
            return await browser.new_context()

        async with PagePool(
            browser=browser,
            new_context_func=create_context,
            min_idle_pages=2,
            max_total_pages=10,
        ) as pool:
            assert pool._started is True

            async with pool.page() as page:
                await page.goto("about:blank")

        # Pool should be stopped after context manager exit
        assert pool._started is False


class TestConcurrency:
    """Test concurrent page usage."""

    @pytest.mark.asyncio
    async def test_concurrent_page_usage(self, simple_pool: PagePool):
        """Test using multiple pages concurrently."""

        async def use_page(url: str):
            async with simple_pool.page() as page:
                await page.goto(url)
                return await page.title()

        urls = [
            "data:text/html,<title>Page 1</title>",
            "data:text/html,<title>Page 2</title>",
            "data:text/html,<title>Page 3</title>",
            "data:text/html,<title>Page 4</title>",
            "data:text/html,<title>Page 5</title>",
        ]

        # Use pages concurrently
        results = await asyncio.gather(*[use_page(url) for url in urls])

        assert len(results) == 5
        assert "Page 1" in results
        assert "Page 5" in results

        # All pages should be released
        stats = simple_pool.get_stats()
        assert stats["active_pages"] == 0


class TestStatistics:
    """Test pool statistics."""

    @pytest.mark.asyncio
    async def test_get_stats(self, simple_pool: PagePool):
        """Test get_stats() returns correct information."""
        stats = simple_pool.get_stats()

        assert "total_contexts" in stats
        assert "draining_contexts" in stats
        assert "idle_pages" in stats
        assert "active_pages" in stats
        assert "total_pages" in stats
        assert "started" in stats

        assert stats["started"] is True
        assert stats["total_contexts"] >= 1
        assert stats["idle_pages"] >= 0
        assert stats["total_pages"] == stats["idle_pages"] + stats["active_pages"]

    @pytest.mark.asyncio
    async def test_stats_update_on_get_release(self, simple_pool: PagePool):
        """Test that stats update correctly on get/release."""
        initial_stats = simple_pool.get_stats()
        initial_active = initial_stats["active_pages"]

        # Get a page
        page_wrapper = await simple_pool.get_page()

        stats_after_get = simple_pool.get_stats()
        assert stats_after_get["active_pages"] == initial_active + 1

        # Release the page
        await simple_pool.release_page(page_wrapper)
        await asyncio.sleep(0.1)

        stats_after_release = simple_pool.get_stats()
        assert stats_after_release["active_pages"] == initial_active
