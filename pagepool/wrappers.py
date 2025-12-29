"""Lightweight wrappers around Playwright objects for uniform lifecycle management."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable
import asyncio

from playwright.async_api import Browser, BrowserContext, Page, Playwright

from .config import ConnectionStats, PoolConfig
from .exceptions import EndpointConnectionError


@dataclass
class BrowserWrapper:
    """Wraps Playwright Browser connection with lifecycle and stats."""

    endpoint: str
    playwright: Playwright
    config: PoolConfig
    stats: ConnectionStats
    obj: Browser | None = None
    created_at: datetime | None = None
    _lock: asyncio.Lock = field(init=False, repr=False)
    _is_connected: bool = False
    on_disconnect: Callable[[str], Awaitable[None]] | None = None
    _loop: asyncio.AbstractEventLoop | None = field(init=False, repr=False, default=None)

    # Page pool management
    contexts: list[ContextWrapper] = field(default_factory=list, init=False, repr=False)
    idle_pages: asyncio.Queue[PageWrapper] = field(init=False, repr=False)
    _refill_pending: bool = field(default=False, init=False, repr=False)

    def __post_init__(self):
        self._lock = asyncio.Lock()
        self.idle_pages = asyncio.Queue()
        self.contexts = []
        self._refill_pending = False

    def age_seconds(self) -> float:
        if not self.created_at:
            return 0.0
        return (datetime.now() - self.created_at).total_seconds()

    async def connect(self) -> "BrowserWrapper":
        async with self._lock:
            if self._is_connected and self.obj:
                try:
                    _ = self.obj.contexts
                    return self
                except Exception:
                    self._is_connected = False
                    self.obj = None

            try:
                browser = await self.playwright.chromium.connect_over_cdp(
                    self.endpoint,
                    timeout=self.config.connection_timeout * 1000,
                    **self.config.cdp_connect_opts,
                )
                self.obj = browser
                self.created_at = datetime.now()
                self._is_connected = True
                self._loop = asyncio.get_running_loop()

                def _handle_disconnected() -> None:
                    self.stats.is_healthy = False
                    self.stats.circuit_state = "open"
                    self.stats.active_connections = 0
                    self._is_connected = False
                    self.obj = None
                    if self.on_disconnect and self._loop:
                        self._loop.create_task(self.on_disconnect(self.endpoint))

                browser.on("disconnected", _handle_disconnected)
                self.stats.is_healthy = True
                self.stats.total_connections += 1
                self.stats.active_connections = 1
                self.stats.success_count += 1
                return self
            except Exception as e:  # noqa: BLE001
                self.stats.is_healthy = False
                self.stats.last_error = str(e)
                self.stats.error_count += 1
                raise EndpointConnectionError(
                    f"Failed to connect to {self.endpoint}: {e}"
                ) from e

    async def disconnect(self) -> None:
        async with self._lock:
            if self.obj:
                try:
                    await self.obj.close()
                except Exception:
                    pass
                finally:
                    self.obj = None
                    self._is_connected = False
                    self.stats.active_connections = 0

    async def health_check(self) -> bool:
        try:
            if not self._is_connected or not self.obj:
                return False
            _ = self.obj.contexts
            self.stats.is_healthy = True
            self.stats.success_count += 1
            return True
        except Exception as e:  # noqa: BLE001
            self.stats.is_healthy = False
            self.stats.last_error = str(e)
            self.stats.error_count += 1
            return False

    @property
    def is_healthy(self) -> bool:
        return self.stats.is_healthy

    def _select_context_with_capacity(self) -> ContextWrapper | None:
        """Pick the first context with remaining page capacity."""
        for record in self.contexts:
            if (
                not record.draining
                and not record.ttl_expired(self.config.context_ttl)
                and (
                    self.config.max_pages_per_context == 0
                    or record.active_pages < self.config.max_pages_per_context
                )
            ):
                return record
        return None

    def _can_create_more_pages(self) -> bool:
        """Check if we can create more pages based on config limits."""
        if self.config.max_idle_pages and self.idle_pages.qsize() >= self.config.max_idle_pages:
            return False
        return True

    async def _create_context(self) -> ContextWrapper:
        """Create and register a new context for this browser."""
        import logging

        try:
            browser_wrapper = await self.connect()
        except Exception as exc:
            logging.warning("connect failed for %s: %s", self.endpoint, exc)
            raise
        if browser_wrapper.obj is None:
            from .exceptions import PageAcquireError

            raise PageAcquireError("Browser connection did not return an active browser")
        try:
            if self.config.context_factory:
                context = await self.config.context_factory(browser_wrapper.obj)
            else:
                context = await browser_wrapper.obj.new_context()
        except Exception as exc:
            logging.warning("create context failed for %s: %s", self.endpoint, exc)
            raise

        record = ContextWrapper(obj=context, endpoint=self.endpoint, created_at=datetime.now())
        self.contexts.append(record)

        self.stats.total_contexts += 1
        self.stats.active_contexts = len(self.contexts)
        return record

    async def _discard_page(self, page: Page):
        """Close a page quietly."""
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass

    async def _discard_context(self, ctx_record: ContextWrapper):
        """Close and remove a context."""
        try:
            await ctx_record.obj.close()
        except Exception:
            pass
        finally:
            if ctx_record in self.contexts:
                self.contexts.remove(ctx_record)
                self.stats.active_contexts = len(self.contexts)
            # Purge idle pages tied to this context
            remaining: asyncio.Queue[PageWrapper] = asyncio.Queue()
            while not self.idle_pages.empty():
                try:
                    wrapper = self.idle_pages.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if wrapper.context == ctx_record:
                    await self._discard_page(wrapper.obj)
                else:
                    await remaining.put(wrapper)
            self.idle_pages = remaining

    def _check_page(self, page_wrapper: PageWrapper) -> bool:
        """Validate an idle page and mark it as in-use."""
        from .exceptions import PageAcquireError

        ctx_wrapper = page_wrapper.context
        if (
            page_wrapper.obj.is_closed()
            or ctx_wrapper.draining
            or ctx_wrapper.ttl_expired(self.config.context_ttl)
        ):
            asyncio.create_task(self._discard_page(page_wrapper.obj))
            if ctx_wrapper.active_pages == 0 and (
                ctx_wrapper.draining or ctx_wrapper.ttl_expired(self.config.context_ttl)
            ):
                asyncio.create_task(self._discard_context(ctx_wrapper))
            return False

        ctx_wrapper.inc_pages()
        self.stats.active_pages += 1
        return True

    async def _spawn_idle_page(self, scene: str | None = None):
        """Create a page and return it to the idle queue without counting as active."""
        import logging

        if self.config.min_active_page <= 0:
            return

        context_with_capacity = self._select_context_with_capacity()
        if not context_with_capacity:
            if not self._can_create_more_pages():
                return
            try:
                context_with_capacity = await self._create_context()
            except Exception as exc:
                logging.warning("spawn: create context failed for %s: %s", self.endpoint, exc)
                return

        if context_with_capacity.draining or context_with_capacity.ttl_expired(
            self.config.context_ttl
        ):
            return

        try:
            page = await context_with_capacity.obj.new_page()
        except Exception as exc:
            logging.warning("spawn: new page failed for %s: %s", self.endpoint, exc)
            return

        try:
            if self.config.page_init:
                if isinstance(self.config.page_init, dict):
                    initializer = (
                        self.config.page_init.get(scene)
                        if scene
                        else self.config.page_init.get("default")
                        or next(iter(self.config.page_init.values()), None)
                    )
                else:
                    initializer = self.config.page_init
                if initializer:
                    await initializer(page)
        except Exception as exc:
            logging.warning("spawn: page init failed for %s: %s", self.endpoint, exc)
            await self._discard_page(page)
            return

        page_wrapper = PageWrapper(
            obj=page,
            context=context_with_capacity,
            created_at=datetime.now(),
        )
        await self.idle_pages.put(page_wrapper)
        self.stats.total_pages += 1

    async def _refill_pending_pages(self):
        """Refill idle pages to meet min_active_page target."""
        import logging

        try:
            target = self.config.min_active_page
            if self.config.max_idle_pages:
                target = min(target, self.config.max_idle_pages)
            deficit = target - self.idle_pages.qsize()
            if deficit <= 0:
                return
            logging.info("refill %s: deficit=%d", self.endpoint, deficit)
            for _ in range(deficit):
                await self._spawn_idle_page()
        finally:
            self._refill_pending = False

    def _try_fill_page(self):
        """Schedule background refill if needed."""
        if self.config.min_active_page <= 0:
            return
        if not self._can_create_more_pages():
            return
        target = self.config.min_active_page
        if self.config.max_idle_pages:
            target = min(target, self.config.max_idle_pages)
        deficit = target - self.idle_pages.qsize()
        if deficit <= 0:
            return
        if self._refill_pending:
            return
        self._refill_pending = True
        asyncio.create_task(self._refill_pending_pages())

    def acquire_page(self, scene: str | None = None) -> PageWrapper:
        """Acquire a page from this browser's idle pool.

        Returns a usable page or creates a new one if needed.
        Schedules background refill when idle queue is low.

        Returns:
            PageWrapper containing the Page object.

        Raises:
            PageAcquireError: If unable to acquire a page.
        """
        from .exceptions import PageAcquireError

        # Try to get a valid page from idle queue
        while not self.idle_pages.empty():
            try:
                page_wrapper = self.idle_pages.get_nowait()
            except asyncio.QueueEmpty:
                break

            if self._check_page(page_wrapper):
                self._try_fill_page()
                return page_wrapper

        # No idle pages available
        raise PageAcquireError("No idle pages available")

    async def release_page(self, page_wrapper: PageWrapper):
        """Release a page back to the idle pool or discard it."""
        if page_wrapper.released:
            return

        page_wrapper.mark_released()
        ctx_wrapper = page_wrapper.context

        self.stats.active_pages = max(0, self.stats.active_pages - 1)
        ctx_wrapper.dec_pages()

        if ctx_wrapper.draining and ctx_wrapper.active_pages == 0:
            await self._discard_context(ctx_wrapper)
            return

        # Discard closed or expired pages
        context_expired = ctx_wrapper.ttl_expired(self.config.context_ttl)
        if page_wrapper.obj.is_closed() or context_expired:
            await self._discard_page(page_wrapper.obj)
            if context_expired and ctx_wrapper.active_pages == 0:
                await self._discard_context(ctx_wrapper)
            return

        # No page reuse; discard after use and refill idle queue if needed.
        await self._discard_page(page_wrapper.obj)
        if context_expired and ctx_wrapper.active_pages == 0:
            await self._discard_context(ctx_wrapper)
        self._try_fill_page()

    async def warmup(self):
        """Warmup the browser by creating initial idle pages."""
        import logging

        if self.config.min_active_page <= 0:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10.0
        target = self.config.min_active_page
        if self.config.max_idle_pages:
            target = min(target, self.config.max_idle_pages)
        logging.info(
            "warmup start %s: idle=%d target=%d", self.endpoint, self.idle_pages.qsize(), target
        )
        while self.idle_pages.qsize() < target and loop.time() < deadline:
            await self._refill_pending_pages()
            await asyncio.sleep(0.1)
        if self.idle_pages.qsize() < target:
            logging.debug(
                "warmup incomplete for %s: idle=%d target=%d",
                self.endpoint,
                self.idle_pages.qsize(),
                target,
            )
        else:
            logging.info("warmup complete %s: idle=%d", self.endpoint, self.idle_pages.qsize())

    async def _purge_idle_pages_for_context(self, ctx_record: ContextWrapper) -> None:
        """Purge idle pages that belong to a specific context."""
        remaining: asyncio.Queue[PageWrapper] = asyncio.Queue()
        while not self.idle_pages.empty():
            try:
                wrapper = self.idle_pages.get_nowait()
            except asyncio.QueueEmpty:
                break
            if wrapper.context == ctx_record:
                await self._discard_page(wrapper.obj)
            else:
                await remaining.put(wrapper)
        self.idle_pages = remaining

    async def rotate_expiring_contexts(self) -> None:
        """Rotate contexts that are about to expire."""
        ttl = self.config.context_ttl
        if ttl is None or ttl <= 0:
            return
        lead = min(5.0, ttl * 0.1)
        now = datetime.now()
        for record in list(self.contexts):
            if record.draining:
                continue
            age = (now - record.created_at).total_seconds()
            if age < (ttl - lead):
                continue
            try:
                await self._create_context()
            except Exception:
                continue
            record.draining = True
            await self._purge_idle_pages_for_context(record)
            if record.active_pages == 0:
                await self._discard_context(record)

    async def cleanup(self):
        """Cleanup all contexts and idle pages for this browser."""
        import logging

        logging.info("browser cleanup: %s", self.endpoint)
        for record in list(self.contexts):
            await self._discard_context(record)
        self.contexts.clear()

        while not self.idle_pages.empty():
            try:
                wrapper = self.idle_pages.get_nowait()
            except asyncio.QueueEmpty:
                break
            await self._discard_page(wrapper.obj)

    async def handle_disconnect(self):
        """Handle browser disconnection."""
        import logging

        logging.warning("endpoint disconnected: %s", self.endpoint)
        self.stats.is_healthy = False
        self.stats.circuit_state = "open"
        self.stats.last_error = "cdp disconnected"
        self.stats.active_connections = 0
        self.stats.active_pages = 0

        for record in list(self.contexts):
            await self._discard_context(record)

        while not self.idle_pages.empty():
            try:
                wrapper = self.idle_pages.get_nowait()
            except asyncio.QueueEmpty:
                break
            await self._discard_page(wrapper.obj)


@dataclass
class ContextWrapper:
    """Wraps a Playwright BrowserContext instance with metadata."""

    obj: BrowserContext
    endpoint: str
    created_at: datetime
    active_pages: int = 0
    draining: bool = False

    @property
    def context(self) -> BrowserContext:
        return self.obj

    def inc_pages(self) -> None:
        self.active_pages += 1

    def dec_pages(self) -> None:
        self.active_pages = max(0, self.active_pages - 1)

    def ttl_expired(self, ttl: float | None) -> bool:
        if ttl is None:
            return False
        return (datetime.now() - self.created_at).total_seconds() > ttl


@dataclass
class PageWrapper:
    """Wraps a Playwright Page instance."""

    obj: Page
    context: "ContextWrapper"
    created_at: datetime
    released: bool = False
    _releaser: Callable[["PageWrapper"], Awaitable[None]] | None = None

    @property
    def page(self) -> Page:
        """Backward-compatible accessor for the underlying page."""
        return self.obj

    @property
    def context_obj(self) -> BrowserContext:
        """Access the underlying BrowserContext."""
        return self.context.obj

    def mark_released(self) -> None:
        self.released = True

    def is_ttl_expired(self, ttl_seconds: float | None) -> bool:
        """Check if page exceeded a TTL."""
        if ttl_seconds is None:
            return False
        return (datetime.now() - self.created_at).total_seconds() > ttl_seconds

    async def release(self) -> None:
        """Release via provided releaser callback if set."""
        if self.released:
            return
        self.released = True
        if self._releaser:
            await self._releaser(self)
