"""Main page pool implementation for high-concurrency web scraping."""

import asyncio
from typing import Dict, Optional
from contextlib import asynccontextmanager
from playwright.async_api import async_playwright, Playwright

from .config import PoolConfig, ConnectionStats
from .connection import BrowserConnection
from .context_pool import ContextPool
from .page_wrapper import PageWrapper
from .load_balancer import LoadBalancer
from .exceptions import PageAcquireError


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

        self._playwright: Optional[Playwright] = None
        self._connections: Dict[str, BrowserConnection] = {}
        self._context_pools: Dict[str, ContextPool] = {}
        self._load_balancer: Optional[LoadBalancer] = None

        self._started = False
        self._health_check_task: Optional[asyncio.Task] = None
        self._endpoints_refresh_task: Optional[asyncio.Task] = None
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

            # Start health check task
            self._health_check_task = asyncio.create_task(self._health_check_loop())

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

            # Stop health check task
            if self._health_check_task:
                self._health_check_task.cancel()
                try:
                    await self._health_check_task
                except asyncio.CancelledError:
                    pass

            # Stop endpoints refresh task
            if self._endpoints_refresh_task:
                self._endpoints_refresh_task.cancel()
                try:
                    await self._endpoints_refresh_task
                except asyncio.CancelledError:
                    pass

            # Stop all context pools
            for context_pool in self._context_pools.values():
                await context_pool.stop()

            # Disconnect all browsers
            for connection in self._connections.values():
                await connection.disconnect()

            # Stop Playwright
            if self._playwright:
                await self._playwright.stop()

            self._started = False

    async def _refresh_endpoints(self):
        """Refresh endpoint list, adding new endpoints and removing old ones."""
        # Get current endpoint list
        if callable(self.config.cdp_endpoints):
            if asyncio.iscoroutinefunction(self.config.cdp_endpoints):
                current_endpoints = await self.config.cdp_endpoints()
            else:
                current_endpoints = self.config.cdp_endpoints()
        else:
            current_endpoints = self.config.cdp_endpoints

        current_endpoints_set = set(current_endpoints)
        existing_endpoints_set = set(self._connections.keys())

        # Add new endpoints
        new_endpoints = current_endpoints_set - existing_endpoints_set
        for endpoint in new_endpoints:
            connection = BrowserConnection(
                endpoint=endpoint,
                playwright=self._playwright,
                config=self.config
            )
            self._connections[endpoint] = connection

            context_pool = ContextPool(connection, self.config)
            self._context_pools[endpoint] = context_pool
            await context_pool.start()

        # Remove deleted endpoints
        removed_endpoints = existing_endpoints_set - current_endpoints_set
        for endpoint in removed_endpoints:
            # Gracefully shut down
            context_pool = self._context_pools.pop(endpoint, None)
            if context_pool:
                await context_pool.stop()

            connection = self._connections.pop(endpoint, None)
            if connection:
                await connection.disconnect()

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
        endpoint = self._load_balancer.select_endpoint(stats)

        # Get connection and context pool
        connection = self._connections[endpoint]
        context_pool = self._context_pools[endpoint]

        # Acquire context
        try:
            context = await context_pool.acquire_context()
        except Exception as e:
            raise PageAcquireError(f"Failed to acquire context: {e}") from e

        # Create new page
        try:
            page = await context.new_page()

            # Update stats
            connection.stats.total_pages += 1
            connection.stats.active_pages += 1

            return PageWrapper(page, context, self)

        except Exception as e:
            # Release context on failure
            await context_pool.release_context(context)
            raise PageAcquireError(f"Failed to create page: {e}") from e

    async def release_page(self, page_wrapper: PageWrapper):
        """Release a page back to the pool.

        Args:
            page_wrapper: The PageWrapper to release.
        """
        # Close the page
        await page_wrapper.close()

        # Find which endpoint this context belongs to
        for endpoint, context_pool in self._context_pools.items():
            connection = self._connections[endpoint]

            # Check if context belongs to this pool
            if page_wrapper.context in [w.context for w in context_pool._contexts]:
                connection.stats.active_pages -= 1
                await context_pool.release_context(page_wrapper.context)
                break

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

    async def _health_check_loop(self):
        """Background task for periodic health checks."""
        while True:
            try:
                await asyncio.sleep(self.config.health_check_interval)

                # Run health checks on all connections
                tasks = [
                    connection.health_check()
                    for connection in self._connections.values()
                ]
                await asyncio.gather(*tasks, return_exceptions=True)

            except asyncio.CancelledError:
                break
            except Exception:
                pass  # Continue health check loop

    async def __aenter__(self):
        """Context manager support for the pool itself."""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager cleanup."""
        await self.stop()

    def __repr__(self) -> str:
        return f"<PlaywrightPagePool endpoints={len(self._connections)} started={self._started}>"
