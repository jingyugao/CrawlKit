"""Main page pool implementation for high-concurrency web scraping."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime
from typing import Dict, Tuple, cast
from contextlib import asynccontextmanager
from playwright.async_api import (
    async_playwright,
    Playwright,
    BrowserContext,
    Page,
)

from .config import PoolConfig, ConnectionStats
from .wrappers import BrowserWrapper, ContextWrapper, PageWrapper
from .load_balancer import LoadBalancer
from .exceptions import PageAcquireError, NoHealthyEndpointsError


class PlaywrightPagePool:
    """Main page pool implementation for high-concurrency web scraping.

    Manages multiple CDP connections to remote Chrome instances with
    custom load balancing and context factories.

    Args:
        config: Pool configuration.

    Example:
        >>> config = PoolConfig(cdp_endpoints=['http://localhost:9222'])
        >>> async with PlaywrightPagePool(config) as pool:
        ...     async with pool.page() as page:
        ...         await page.goto('https://example.com')
    """

    def __init__(self, config: PoolConfig):
        self.config = config

        self._playwright: Playwright | None = None
        self._connections: Dict[str, BrowserWrapper] = {}
        self._endpoint_contexts: Dict[str, list[ContextWrapper]] = {}
        self._idle_pages: Dict[str, asyncio.Queue[PageWrapper]] = {}
        self._load_balancer: LoadBalancer | None = None

        self._started = False
        self._endpoints_refresh_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self):
        """Initialize the pool and connect to remote Chrome instances."""
        async with self._lock:
            if self._started:
                return

            # Start Playwright
            self._playwright = await async_playwright().start()

            # Initialize endpoint list
            await self._refresh_endpoints()

            # Start endpoint refresh task (if cdp_endpoints is a function)
            if callable(self.config.cdp_endpoints):
                self._endpoints_refresh_task = asyncio.create_task(
                    self._endpoints_refresh_loop()
                )

            self._started = True

    async def stop(self):
        """Shutdown the pool and cleanup all resources."""
        async with self._lock:
            if not self._started:
                return

            # Stop endpoints refresh task
            if self._endpoints_refresh_task:
                self._endpoints_refresh_task.cancel()
                try:
                    await self._endpoints_refresh_task
                except asyncio.CancelledError:
                    pass

            # Cleanup endpoint resources
            for endpoint in list(self._connections.keys()):
                await self._cleanup_endpoint(endpoint)

            # Stop Playwright
            if self._playwright:
                await self._playwright.stop()

            self._started = False

    async def _refresh_endpoints(self):
        """Refresh endpoint list, adding new endpoints and removing old ones."""
        current_endpoints = await self._resolve_endpoints()

        current_endpoints_set = set(current_endpoints)
        existing_endpoints_set = set(self._connections.keys())

        # Add new endpoints
        new_endpoints = current_endpoints_set - existing_endpoints_set
        for endpoint in new_endpoints:
            assert self._playwright is not None, "Playwright instance should be initialized before refreshing endpoints"
            wrapper = BrowserWrapper(
                endpoint=endpoint,
                playwright=self._playwright,
                config=self.config,
                stats=ConnectionStats(endpoint=endpoint),
            )
            self._connections[endpoint] = wrapper
            self._endpoint_contexts[endpoint] = []
            self._idle_pages[endpoint] = asyncio.Queue()
            self._schedule_spawn_pages(endpoint)

        # Remove deleted endpoints
        removed_endpoints = existing_endpoints_set - current_endpoints_set
        for endpoint in removed_endpoints:
            await self._cleanup_endpoint(endpoint)

        # Update load balancer with current endpoints
        self._load_balancer = LoadBalancer(
            list(current_endpoints),
            self.config.load_balancer
        )

    async def _endpoints_refresh_loop(self):
        """Background task to periodically refresh endpoint list."""
        while True:
            try:
                await asyncio.sleep(self.config.endpoints_refresh_interval)
                await self._refresh_endpoints()
            except asyncio.CancelledError:
                break
            except Exception:
                pass  # Continue refresh loop

    async def acquire_page(self) -> PageWrapper:
        """Acquire a page from the pool.

        This method:
        1. Selects an endpoint using the load balancer
        2. Gets a context from the context pool
        3. Creates a new page in that context
        4. Returns a PageWrapper for lifecycle management

        Returns:
            PageWrapper containing the Page object.

        Raises:
            PageAcquireError: If unable to acquire a page.
        """
        if not self._started:
            await self.start()

        # Select endpoint using load balancer
        stats = self.get_stats()
        if not self._load_balancer:
            raise PageAcquireError("Pool has no configured load balancer or endpoints")

        try:
            endpoint = self._load_balancer.select_endpoint(stats)
        except NoHealthyEndpointsError as exc:
            raise PageAcquireError("No healthy endpoints available") from exc

        # Get connection for endpoint
        connection = self._connections[endpoint]

        try:
            page_wrapper = await self._acquire_page_for_endpoint(endpoint, connection)
            page_wrapper._releaser = self.release_page
            self._schedule_spawn_pages(endpoint)
            return page_wrapper
        except Exception as e:
            raise PageAcquireError(f"Failed to acquire page: {e}") from e

    async def release_page(self, page_wrapper: PageWrapper):
        """Release a page back to the pool.

        Args:
            page_wrapper: The PageWrapper to release.
        """
        if page_wrapper.released:
            return
        page_wrapper.mark_released()
        await self._release_page_to_endpoint(page_wrapper)

    @asynccontextmanager
    async def page(self):
        """Context manager for acquiring and releasing pages.

        Yields:
            Page instance.

        Example:
            >>> async with pool.page() as page:
            ...     await page.goto('https://example.com')
        """
        page_wrapper = await self.acquire_page()
        try:
            yield page_wrapper.page
        finally:
            await page_wrapper.release()

    def get_stats(self) -> Dict[str, ConnectionStats]:
        """Get current statistics for all endpoints.

        Returns:
            Dictionary mapping endpoint URLs to ConnectionStats.
        """
        return {
            endpoint: connection.stats
            for endpoint, connection in self._connections.items()
        }

    def get_health(self) -> Dict[str, bool]:
        """Get health status for all endpoints.

        Returns:
            Dictionary mapping endpoint URLs to health status.
        """
        return {
            endpoint: connection.is_healthy
            for endpoint, connection in self._connections.items()
        }

    async def __aenter__(self):
        """Context manager support for the pool itself."""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager cleanup."""
        await self.stop()

    def __repr__(self) -> str:
        return f"<PlaywrightPagePool endpoints={len(self._connections)} started={self._started}>"

    async def _resolve_endpoints(self) -> list[str]:
        """Resolve configured endpoints into a concrete list."""
        endpoints = self.config.cdp_endpoints
        if callable(endpoints):
            if asyncio.iscoroutinefunction(endpoints):
                resolved = await endpoints()
            else:
                resolved = endpoints()
        else:
            resolved = endpoints

        if isinstance(resolved, str):
            raise ValueError("cdp_endpoints must not be a single string")

        resolved_iterable = cast(Iterable[str], resolved)
        return list(resolved_iterable)

    async def _acquire_page_for_endpoint(
        self,
        endpoint: str,
        connection: BrowserWrapper,
    ) -> PageWrapper:
        """Get a page for the given endpoint, reusing existing pages when possible."""
        idle_queue = self._idle_pages[endpoint]
        contexts = self._endpoint_contexts[endpoint]

        # 1) Try reusing an idle page
        page_wrapper = await self._pop_usable_idle_page(idle_queue, endpoint, connection)
        if page_wrapper:
            return page_wrapper

        # 2) Pick or create a context with capacity
        context_with_capacity = self._select_context_with_capacity(contexts)
        if not context_with_capacity and len(contexts) < self.config.max_contexts_per_connection:
            context_with_capacity = await self._create_context(endpoint, connection)

        # 3) If still no context, wait for an idle page to show up
        if context_with_capacity is None:
            try:
                page_wrapper = await asyncio.wait_for(
                    idle_queue.get(),
                    timeout=self.config.acquire_timeout,
                )
            except asyncio.TimeoutError as e:
                raise PageAcquireError(
                    f"Timeout acquiring page after {self.config.acquire_timeout}s"
                ) from e

            activated = await self._activate_idle_page(page_wrapper, endpoint, connection)
            if activated is None:
                return await self._acquire_page_for_endpoint(endpoint, connection)
            return activated

        # 4) Create a new page on the selected context
        try:
            page = await context_with_capacity.obj.new_page()
        except Exception as e:  # noqa: BLE001
            raise PageAcquireError(f"Failed to create page: {e}") from e

        page_wrapper = PageWrapper(
            obj=page,
            context=context_with_capacity,
            created_at=datetime.now(),
        )

        context_with_capacity.inc_pages()
        connection.stats.total_pages += 1
        connection.stats.active_pages += 1
        return page_wrapper

    async def _pop_usable_idle_page(
        self,
        idle_queue: asyncio.Queue[PageWrapper],
        endpoint: str,
        connection: BrowserWrapper,
    ) -> PageWrapper | None:
        """Return a usable idle page if available, updating counts."""
        while not idle_queue.empty():
            try:
                page_wrapper = idle_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            ctx_wrapper = page_wrapper.context

            if self._activate_idle_page(page_wrapper, endpoint, connection, mutate_stats=False):
                return page_wrapper

        return None

    async def _activate_idle_page(
        self,
        page_wrapper: PageWrapper,
        endpoint: str,
        connection: BrowserWrapper,
        mutate_stats: bool = True,
    ) -> PageWrapper | None:
        """Validate and mark an idle page as in-use."""
        ctx_wrapper = page_wrapper.context
        if page_wrapper.obj.is_closed() or ctx_wrapper.ttl_expired(self.config.context_ttl):
            await self._discard_page(page_wrapper.obj)
            if ctx_wrapper.active_pages == 0 and ctx_wrapper.ttl_expired(self.config.context_ttl):
                await self._discard_context(ctx_wrapper, endpoint)
            return None

        ctx_wrapper.inc_pages()
        if mutate_stats:
            connection.stats.active_pages += 1
        return page_wrapper

    async def _release_page_to_endpoint(self, page_wrapper: PageWrapper):
        """Return a page to the idle pool or discard it."""
        endpoint, ctx_wrapper = self._find_context_record(page_wrapper.context)
        if endpoint is None or ctx_wrapper is None:
            return

        connection = self._connections[endpoint]
        connection.stats.active_pages = max(0, connection.stats.active_pages - 1)

        ctx_wrapper.dec_pages()

        # Discard closed or expired pages
        context_expired = ctx_wrapper.ttl_expired(self.config.context_ttl)
        page_expired = page_wrapper.is_ttl_expired(self.config.page_ttl)

        if page_wrapper.obj.is_closed() or page_expired or context_expired:
            await self._discard_page(page_wrapper.obj)
            if context_expired and ctx_wrapper.active_pages == 0:
                await self._discard_context(ctx_wrapper, endpoint)
            return

        # Reuse page by returning to idle queue
        await self._idle_pages[endpoint].put(page_wrapper)
        self._schedule_spawn_pages(endpoint)

    def _select_context_with_capacity(self, contexts: list[ContextWrapper]) -> ContextWrapper | None:
        """Pick the first context with remaining page capacity."""
        for record in contexts:
            if not record.ttl_expired(self.config.context_ttl) and record.active_pages < self.config.max_pages_per_context:
                return record
        return None

    async def _create_context(self, endpoint: str, connection: BrowserWrapper) -> ContextWrapper:
        """Create and register a new context for an endpoint."""
        browser_wrapper = await connection.connect()
        if browser_wrapper.obj is None:
            raise PageAcquireError("Browser connection did not return an active browser")
        if self.config.context_factory:
            context = await self.config.context_factory(browser_wrapper.obj)
        else:
            context = await browser_wrapper.obj.new_context()

        record = ContextWrapper(obj=context, endpoint=endpoint, created_at=datetime.now())
        self._endpoint_contexts[endpoint].append(record)

        connection.stats.total_contexts += 1
        connection.stats.active_contexts = len(self._endpoint_contexts[endpoint])
        return record

    async def _spawn_idle_page(self, endpoint: str):
        """Create a page and return it to the idle queue without counting as active."""
        if self.config.min_active_page <= 0:
            return

        connection = self._connections[endpoint]
        contexts = self._endpoint_contexts[endpoint]

        context_with_capacity = self._select_context_with_capacity(contexts)
        if not context_with_capacity:
            if len(contexts) >= self.config.max_contexts_per_connection:
                return
            try:
                context_with_capacity = await self._create_context(endpoint, connection)
            except Exception:
                return

        if context_with_capacity.ttl_expired(self.config.context_ttl):
            await self._discard_context(context_with_capacity, endpoint)
            return

        try:
            page = await context_with_capacity.obj.new_page()
        except Exception:
            return

        page_wrapper = PageWrapper(
            obj=page,
            context=context_with_capacity,
            created_at=datetime.now(),
        )
        await self._idle_pages[endpoint].put(page_wrapper)
        connection.stats.total_pages += 1

    def _schedule_spawn_pages(self, endpoint: str):
        """Schedule background creation of idle pages to meet min_active_page."""
        if self.config.min_active_page <= 0:
            return
        idle_queue = self._idle_pages.get(endpoint)
        if idle_queue is None:
            return

        deficit = self.config.min_active_page - idle_queue.qsize()
        if deficit <= 0:
            return

        for _ in range(deficit):
            asyncio.create_task(self._spawn_idle_page(endpoint))

    async def _discard_page(self, page: Page):
        """Close a page quietly."""
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass

    async def _discard_context(self, ctx_record: ContextWrapper, endpoint: str):
        """Close and remove a context."""
        try:
            await ctx_record.obj.close()
        except Exception:
            pass
        finally:
            contexts = self._endpoint_contexts.get(endpoint, [])
            if ctx_record in contexts:
                contexts.remove(ctx_record)
                connection = self._connections[endpoint]
                connection.stats.active_contexts = len(contexts)
            # Purge idle pages tied to this context
            idle_queue = self._idle_pages.get(endpoint)
            if idle_queue:
                remaining: asyncio.Queue[PageWrapper] = asyncio.Queue()
                while not idle_queue.empty():
                    try:
                        wrapper = idle_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if wrapper.context == ctx_record.obj:
                        await self._discard_page(wrapper.obj)
                    else:
                        await remaining.put(wrapper)
                self._idle_pages[endpoint] = remaining

    def _find_context_record(self, context: BrowserContext | ContextWrapper) -> Tuple[str | None, ContextWrapper | None]:
        """Find the endpoint and context record for the given context."""
        if isinstance(context, ContextWrapper):
            return context.endpoint, context

        for endpoint, contexts in self._endpoint_contexts.items():
            for record in contexts:
                if record.obj == context:
                    return endpoint, record
        return None, None

    async def _cleanup_endpoint(self, endpoint: str):
        """Close all contexts and disconnect connection for an endpoint."""
        contexts = self._endpoint_contexts.get(endpoint, [])
        for record in list(contexts):
            await self._discard_context(record, endpoint)
        self._endpoint_contexts.pop(endpoint, None)

        idle_queue = self._idle_pages.pop(endpoint, None)
        if idle_queue:
            while not idle_queue.empty():
                try:
                    wrapper = idle_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                await self._discard_page(wrapper.obj)

        connection = self._connections.pop(endpoint, None)
        if connection:
            await connection.disconnect()
