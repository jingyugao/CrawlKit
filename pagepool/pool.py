"""Simplified PagePool for managing Playwright pages in a single browser."""
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Callable, Awaitable, AsyncIterator
from playwright.async_api import Browser, BrowserContext, Page

from pagepool.wrappers import ContextWrapper, PageWrapper


logger = logging.getLogger(__name__)


class PagePoolError(Exception):
    """Base exception for PagePool errors."""
    pass


class PoolNotStartedError(PagePoolError):
    """Raised when operations are attempted on a stopped pool."""
    pass


class PagePool:
    """Simplified page pool for single browser with context rotation.

    Manages a pool of Playwright pages with the following features:
    - Idle page queue with min/max limits
    - Health checking with retry logic
    - Context TTL rotation
    - Page usage limit tracking
    - Total page limit enforcement

    Example:
        async with PagePool(browser, create_context, min_idle_pages=10) as pool:
            async with pool.page() as page:
                await page.goto('https://example.com')
    """

    def __init__(
        self,
        browser: Browser,
        new_context_func: Callable[[], BrowserContext | Awaitable[BrowserContext]],
        min_idle_pages: int = 5,
        max_total_pages: int = 100,
        max_idle_pages: int = 0,
        max_pages_per_context: int = 0,
        context_ttl: float | None = None,
        max_page_uses: int | None = None,
        health_check: Callable[[PageWrapper], bool | Awaitable[bool]] | None = None,
    ):
        """Initialize PagePool.

        Args:
            browser: Connected Playwright Browser object
            new_context_func: Callable that creates new BrowserContext
            min_idle_pages: Minimum idle pages to maintain (default: 5)
            max_total_pages: Maximum total pages (idle + active) (default: 100)
            max_idle_pages: Maximum idle pages (0 = auto, limited by total) (default: 0)
            max_pages_per_context: Max pages per context (0 = unlimited) (default: 0)
            context_ttl: Context lifetime in seconds (default: None)
            max_page_uses: Max usage count per page (default: None)
            health_check: Custom health check function (default: None)
        """
        self.browser = browser
        self.new_context_func = new_context_func
        self.min_idle_pages = min_idle_pages
        self.max_total_pages = max_total_pages
        self.max_idle_pages = max_idle_pages
        self.max_pages_per_context = max_pages_per_context
        self.context_ttl = context_ttl
        self.max_page_uses = max_page_uses
        self.health_check = health_check

        # State
        self.contexts: list[ContextWrapper] = []
        self.idle_pages: asyncio.Queue[PageWrapper] = asyncio.Queue()
        self._refill_lock: asyncio.Lock = asyncio.Lock()
        self._started: bool = False
        self._startup_lock: asyncio.Lock = asyncio.Lock()

    async def start(self) -> None:
        """Initialize pool and pre-create min_idle_pages."""
        async with self._startup_lock:
            if self._started:
                return

            logger.info("Starting PagePool")
            self._started = True

            # Pre-create idle pages (warmup)
            await self.warmup()

            logger.info(
                f"PagePool started with {self.idle_pages.qsize()} idle pages, "
                f"{len(self.contexts)} contexts"
            )

    async def stop(self) -> None:
        """Close all contexts and clear idle pages."""
        async with self._startup_lock:
            if not self._started:
                return

            logger.info("Stopping PagePool")
            self._started = False

            # Clear idle pages
            while not self.idle_pages.empty():
                try:
                    page_wrapper = self.idle_pages.get_nowait()
                    await self._discard_page(page_wrapper.obj)
                except asyncio.QueueEmpty:
                    break

            # Close all contexts
            for ctx in self.contexts[:]:
                await self._destroy_context(ctx)

            logger.info("PagePool stopped")

    async def __aenter__(self):
        """Context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        await self.stop()

    async def get_page(self) -> PageWrapper:
        """Get a page from the pool.

        Tries to get a healthy page from idle queue (max 3 retries).
        Creates new page if all retries fail or queue is empty.
        Triggers background refill if needed.

        Returns:
            PageWrapper with a healthy page

        Raises:
            PoolNotStartedError: If pool is not started
        """
        if not self._started:
            raise PoolNotStartedError("Pool not started")

        # Try to get healthy page from idle queue (max 3 retries)
        for attempt in range(3):
            if self.idle_pages.empty():
                break

            try:
                page_wrapper = self.idle_pages.get_nowait()
            except asyncio.QueueEmpty:
                break

            # Health check
            is_healthy = await self._check_page_health(page_wrapper)
            if is_healthy:
                # Valid page found
                page_wrapper.increment_use()
                page_wrapper.context.inc_pages()
                page_wrapper.released = False

                # Trigger refill if needed (fire-and-forget)
                await self._trigger_refill_if_needed()

                # Check context TTL rotation
                await self._check_and_rotate_contexts()

                return page_wrapper
            else:
                # Unhealthy, discard and retry
                logger.debug(f"Discarding unhealthy page (attempt {attempt + 1}/3)")
                await self._discard_page(page_wrapper.obj)

        # No healthy page found or queue empty, create new one
        logger.debug("Creating new page (no healthy idle pages available)")
        page_wrapper = await self._create_page_now()

        # Trigger refill if needed
        await self._trigger_refill_if_needed()

        # Check context TTL rotation
        await self._check_and_rotate_contexts()

        return page_wrapper

    async def release_page(self, page_wrapper: PageWrapper) -> None:
        """Release a page back to the pool or destroy it.

        Args:
            page_wrapper: The page wrapper to release
        """
        if page_wrapper.released:
            return

        page_wrapper.mark_released()
        ctx = page_wrapper.context
        ctx.dec_pages()

        # Check if context is draining
        if ctx.draining:
            logger.debug("Discarding page from draining context")
            await self._discard_page(page_wrapper.obj)
            if ctx.active_pages == 0:
                await self._destroy_context(ctx)
            return

        # Check if page closed or max uses exceeded
        if page_wrapper.obj.is_closed() or page_wrapper.is_max_uses_exceeded(self.max_page_uses):
            logger.debug("Discarding page (closed or max uses exceeded)")
            await self._discard_page(page_wrapper.obj)
            return

        # Check if context TTL expired
        if ctx.ttl_expired(self.context_ttl):
            logger.debug("Discarding page from expired context")
            await self._discard_page(page_wrapper.obj)
            if ctx.active_pages == 0:
                await self._destroy_context(ctx)
            return

        # Check total pages limit
        total = self.idle_pages.qsize() + sum(c.active_pages for c in self.contexts)
        if total >= self.max_total_pages:
            logger.debug(f"Discarding page (total limit reached: {total}/{self.max_total_pages})")
            await self._discard_page(page_wrapper.obj)
            return

        # Check idle pages limit (if set)
        if self.max_idle_pages > 0 and self.idle_pages.qsize() >= self.max_idle_pages:
            logger.debug(
                f"Discarding page (idle limit reached: "
                f"{self.idle_pages.qsize()}/{self.max_idle_pages})"
            )
            await self._discard_page(page_wrapper.obj)
            return

        # Clear page and return to pool
        cleared = await self._clear_page(page_wrapper.obj)
        if cleared:
            await self.idle_pages.put(page_wrapper)
            logger.debug(f"Page returned to pool (idle: {self.idle_pages.qsize()})")
        else:
            logger.debug("Failed to clear page, discarding")
            await self._discard_page(page_wrapper.obj)

        # Check context TTL rotation
        await self._check_and_rotate_contexts()

    @asynccontextmanager
    async def page(self) -> AsyncIterator[Page]:
        """Context manager for automatic page acquisition and release.

        Usage:
            async with pool.page() as page:
                await page.goto('https://example.com')
        """
        page_wrapper = await self.get_page()
        try:
            yield page_wrapper.obj
        finally:
            await self.release_page(page_wrapper)

    def get_stats(self) -> dict:
        """Get pool statistics.

        Returns:
            Dictionary with pool stats
        """
        total_active = sum(c.active_pages for c in self.contexts)
        return {
            'total_contexts': len(self.contexts),
            'draining_contexts': sum(1 for c in self.contexts if c.draining),
            'idle_pages': self.idle_pages.qsize(),
            'active_pages': total_active,
            'total_pages': self.idle_pages.qsize() + total_active,
            'started': self._started,
        }

    # Context Management

    async def _create_context(self) -> ContextWrapper:
        """Create a new browser context.

        Returns:
            ContextWrapper for the new context
        """
        # Call user-provided factory
        result: BrowserContext | Awaitable[BrowserContext]
        if asyncio.iscoroutinefunction(self.new_context_func):
            result = await self.new_context_func()
        else:
            result = self.new_context_func()

        # Handle case where sync function returns Awaitable
        context: BrowserContext
        if asyncio.iscoroutine(result):
            context = await result  # type: ignore
        else:
            context = result  # type: ignore

        # Wrap
        ctx_wrapper = ContextWrapper(
            obj=context,
            created_at=datetime.now(),
        )

        self.contexts.append(ctx_wrapper)
        logger.debug(f"Created new context (total: {len(self.contexts)})")
        return ctx_wrapper

    def _select_context_with_capacity(self) -> ContextWrapper | None:
        """Find a context that can accept more pages.

        Returns:
            ContextWrapper with capacity, or None if none available
        """
        for ctx in self.contexts:
            if ctx.draining:
                continue
            if ctx.ttl_expired(self.context_ttl):
                continue
            if self.max_pages_per_context > 0:
                # Count total pages in this context (active + idle)
                if ctx.active_pages >= self.max_pages_per_context:
                    continue
            return ctx
        return None

    async def _destroy_context(self, ctx: ContextWrapper) -> None:
        """Close and remove a context.

        Args:
            ctx: ContextWrapper to destroy
        """
        try:
            await ctx.obj.close()
            logger.debug("Context closed")
        except Exception as e:
            logger.warning(f"Error closing context: {e}")
        finally:
            if ctx in self.contexts:
                self.contexts.remove(ctx)

    # Page Management

    async def _create_page_now(self) -> PageWrapper:
        """Create a new page immediately (for get_page when pool is empty).

        Returns:
            PageWrapper for the new page
        """
        # Find context with capacity
        ctx = self._select_context_with_capacity()

        # Create new context if needed
        if ctx is None:
            ctx = await self._create_context()

        # Create page
        page = await ctx.obj.new_page()

        # Wrap and return
        page_wrapper = PageWrapper(
            obj=page,
            context=ctx,
            created_at=datetime.now(),
            use_count=1,  # Already being used
            _releaser=self.release_page,
        )

        ctx.inc_pages()
        logger.debug(f"Created new page immediately (context active: {ctx.active_pages})")

        return page_wrapper

    async def _create_idle_page(self) -> None:
        """Create a new page and add to idle pool (for background refill)."""
        # Check max_idle_pages limit
        if self.max_idle_pages > 0 and self.idle_pages.qsize() >= self.max_idle_pages:
            return

        # Check max_total_pages limit
        total = self.idle_pages.qsize() + sum(c.active_pages for c in self.contexts)
        if total >= self.max_total_pages:
            return

        # Find context with capacity
        ctx = self._select_context_with_capacity()

        # Create new context if needed
        if ctx is None:
            ctx = await self._create_context()

        # Don't create on draining context
        if ctx.draining:
            return

        # Create page
        try:
            page = await ctx.obj.new_page()

            # Wrap and add to pool
            page_wrapper = PageWrapper(
                obj=page,
                context=ctx,
                created_at=datetime.now(),
                use_count=0,
                _releaser=self.release_page,
            )

            await self.idle_pages.put(page_wrapper)
            logger.debug(f"Created idle page (idle: {self.idle_pages.qsize()})")
        except Exception as e:
            logger.warning(f"Failed to create idle page: {e}")

    async def _refill_idle_pages(self) -> None:
        """Background task to refill to min_idle_pages (with lock)."""
        async with self._refill_lock:
            target = self.min_idle_pages
            deficit = target - self.idle_pages.qsize()

            if deficit <= 0:
                return

            logger.debug(f"Refilling idle pages (target: {target}, deficit: {deficit})")

            for _ in range(deficit):
                # Check if we should stop
                if not self._started:
                    break
                if self.idle_pages.qsize() >= target:
                    break

                await self._create_idle_page()

    async def _trigger_refill_if_needed(self) -> None:
        """Fire-and-forget background refill task."""
        if self.min_idle_pages <= 0:
            return
        if self.idle_pages.qsize() >= self.min_idle_pages:
            return

        # Check if we can create more pages
        total = self.idle_pages.qsize() + sum(c.active_pages for c in self.contexts)
        if total >= self.max_total_pages:
            return

        # Try to acquire lock (non-blocking)
        if self._refill_lock.locked():
            return  # Already refilling

        # Spawn background task
        asyncio.create_task(self._refill_idle_pages())

    async def _purge_idle_pages_for_context(self, ctx: ContextWrapper) -> None:
        """Remove all idle pages belonging to a specific context.

        Args:
            ctx: ContextWrapper to purge pages for
        """
        remaining: asyncio.Queue[PageWrapper] = asyncio.Queue()

        while not self.idle_pages.empty():
            try:
                page_wrapper = self.idle_pages.get_nowait()
            except asyncio.QueueEmpty:
                break

            if page_wrapper.context == ctx:
                await self._discard_page(page_wrapper.obj)
            else:
                await remaining.put(page_wrapper)

        self.idle_pages = remaining
        logger.debug(f"Purged idle pages for context (remaining: {self.idle_pages.qsize()})")

    async def _clear_page(self, page: Page) -> bool:
        """Navigate to about:blank to clear state.

        Args:
            page: Page to clear

        Returns:
            True if clear succeeded, False if page should be discarded
        """
        try:
            await asyncio.wait_for(
                page.goto("about:blank", wait_until="domcontentloaded"),
                timeout=5.0
            )
            return True
        except Exception as e:
            logger.debug(f"Failed to clear page: {e}")
            return False

    async def _discard_page(self, page: Page) -> None:
        """Close a page, ignoring errors.

        Args:
            page: Page to discard
        """
        try:
            if not page.is_closed():
                await page.close()
        except Exception as e:
            logger.debug(f"Error discarding page: {e}")

    # Health Check

    async def _default_health_check(self, page_wrapper: PageWrapper) -> bool:
        """Default health check: browser connected and page not closed.

        Args:
            page_wrapper: PageWrapper to check

        Returns:
            True if healthy, False otherwise
        """
        try:
            return self.browser.is_connected() and not page_wrapper.obj.is_closed()
        except Exception:
            return False

    async def _check_page_health(self, page_wrapper: PageWrapper) -> bool:
        """Call user-provided health check or default.

        Args:
            page_wrapper: PageWrapper to check

        Returns:
            True if healthy, False otherwise
        """
        try:
            if self.health_check:
                # User provided custom health check
                result: bool | Awaitable[bool]
                if asyncio.iscoroutinefunction(self.health_check):
                    result = await self.health_check(page_wrapper)
                else:
                    result = self.health_check(page_wrapper)

                # Handle case where sync function returns Awaitable
                if asyncio.iscoroutine(result):
                    return await result  # type: ignore
                return bool(result)
            else:
                # Use default
                return await self._default_health_check(page_wrapper)
        except Exception as e:
            logger.debug(f"Health check error: {e}")
            return False

    # Warmup

    async def warmup(self, target: int | None = None) -> None:
        """Pre-create pages during start().

        Args:
            target: Number of pages to create (default: min_idle_pages)
        """
        target = target or self.min_idle_pages
        if target <= 0:
            return

        logger.debug(f"Warming up pool (target: {target} pages)")

        # Batch create (max 10 at a time for efficiency)
        batch_size = min(target, 10)
        while self.idle_pages.qsize() < target:
            remaining = target - self.idle_pages.qsize()
            batch = min(remaining, batch_size)

            tasks = [self._create_idle_page() for _ in range(batch)]
            await asyncio.gather(*tasks, return_exceptions=True)

            # Check if we should stop
            if not self._started:
                break

    # Context TTL Rotation

    async def _check_and_rotate_contexts(self) -> None:
        """Check and rotate expiring contexts.

        Called by get_page() and release_page().
        Finds contexts approaching TTL and initiates rotation.
        """
        if not self.context_ttl:
            return

        # Find contexts approaching TTL (within 10% or 5s, whichever is smaller)
        lead_time = min(5.0, self.context_ttl * 0.1)

        for ctx in self.contexts[:]:  # Copy list to allow modification
            if ctx.draining:
                continue

            age = (datetime.now() - ctx.created_at).total_seconds()
            if age < (self.context_ttl - lead_time):
                continue

            # Context is approaching TTL
            logger.info(f"Context approaching TTL ({age:.1f}s), initiating rotation")

            # 1. Mark as draining
            ctx.draining = True

            # 2. Create new context
            try:
                await self._create_context()
            except Exception as e:
                logger.error(f"Failed to create replacement context: {e}")
                ctx.draining = False  # Revert
                continue

            # 3. Purge idle pages from this context
            await self._purge_idle_pages_for_context(ctx)

            # 4. If no active pages, destroy immediately
            if ctx.active_pages == 0:
                await self._destroy_context(ctx)
