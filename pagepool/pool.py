"""Main page pool implementation for high-concurrency web scraping."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
import json
import logging
from typing import Coroutine, Dict, cast, Any
from urllib.parse import urlparse, urlunparse
from urllib.request import urlopen
from contextlib import contextmanager
from playwright.async_api import Playwright

from .config import PoolConfig, ConnectionStats
from .wrappers import BrowserWrapper, PageWrapper
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
        self._owns_playwright = False
        self._connections: Dict[str, BrowserWrapper] = {}
        self._load_balancer: LoadBalancer | None = None
        self._draining_endpoints: set[str] = set()
        self._monitor_task: asyncio.Task | None = None

        self._started = False
        self._lock = asyncio.Lock()

    async def start(self):
        """Initialize the pool and connect to remote Chrome instances."""
        async with self._lock:
            if self._started:
                return

            # Start Playwright
            self._playwright = self.config.playwright
            self._owns_playwright = False

            # Initialize endpoint list
            await self._refresh_endpoints()

            self._monitor_task = asyncio.create_task(self._monitor_loop())
            await self._warmup_idle_pages()

            self._started = True

    async def stop(self):
        """Shutdown the pool and cleanup all resources."""
        async with self._lock:
            if not self._started:
                return
            if self._monitor_task:
                self._monitor_task.cancel()
                try:
                    await self._monitor_task
                except asyncio.CancelledError:
                    pass
                self._monitor_task = None

            # Cleanup endpoint resources
            for endpoint in list(self._connections.keys()):
                await self._cleanup_endpoint(endpoint)

            # Stop Playwright
            if self._playwright and self._owns_playwright:
                await self._playwright.stop()
            self._playwright = None
            self._owns_playwright = False

            self._started = False

    async def _refresh_endpoints(self):
        """Refresh endpoint list, adding new endpoints and removing old ones."""
        current_endpoints = await self._resolve_endpoints()

        current_endpoints_set = set(current_endpoints)
        existing_endpoints_set = set(self._connections.keys())

        # Add new endpoints
        new_endpoints = current_endpoints_set - existing_endpoints_set
        for endpoint in new_endpoints:
            assert (
                self._playwright is not None
            ), "Playwright instance should be initialized before refreshing endpoints"
            wrapper = BrowserWrapper(
                endpoint=endpoint,
                playwright=self._playwright,
                config=self.config,
                stats=ConnectionStats(endpoint=endpoint),
                on_disconnect=self._handle_endpoint_disconnect,
            )
            self._connections[endpoint] = wrapper
            wrapper._try_fill_page()

        # Remove deleted endpoints
        removed_endpoints = existing_endpoints_set - current_endpoints_set
        for endpoint in removed_endpoints:
            await self._drain_or_cleanup_endpoint(endpoint)

        # Clear draining flag for endpoints that reappear
        for endpoint in current_endpoints_set & self._draining_endpoints:
            self._draining_endpoints.discard(endpoint)
            connection = self._connections.get(endpoint)
            if connection:
                connection.stats.circuit_state = "closed"
                connection.stats.is_healthy = True

        # Update load balancer with current endpoints
        self._load_balancer = LoadBalancer(list(current_endpoints), self.config.load_balancer)

    def acquire_page(self, scene: str | None = None) -> PageWrapper:
        """Acquire a page from the pool with load balancing.

        This method:
        1. Selects an endpoint using the load balancer
        2. Delegates to BrowserWrapper to get a page from its idle queue
        3. Retries other endpoints if one fails

        Returns:
            PageWrapper containing the Page object.

        Raises:
            PageAcquireError: If unable to acquire a page.
        """
        if not self._started:
            raise PageAcquireError("Pool not started; call start() first")

        # Select endpoint using load balancer, retrying other endpoints if one fails
        stats = {
            endpoint: stat
            for endpoint, stat in self.get_stats().items()
            if endpoint not in self._draining_endpoints
        }
        if not self._load_balancer:
            raise PageAcquireError("Pool has no configured load balancer or endpoints")

        last_error: Exception | None = None
        attempts = max(1, len(stats))
        for _ in range(attempts):
            if not stats:
                break
            try:
                endpoint = self._load_balancer.select_endpoint(stats)
            except NoHealthyEndpointsError as exc:
                raise PageAcquireError("No healthy endpoints available") from exc

            connection = self._connections[endpoint]
            try:
                page_wrapper = connection.acquire_page(scene)
                page_wrapper._releaser = self.release_page
                return page_wrapper
            except PageAcquireError as exc:
                last_error = exc
                logging.warning("acquire failed on %s: %s", endpoint, exc)
                connection.stats.is_healthy = False
                connection.stats.circuit_state = "open"
                stats.pop(endpoint, None)
                continue
            except Exception as exc:
                last_error = exc
                logging.warning("acquire unexpected error on %s: %s", endpoint, exc)
                connection.stats.is_healthy = False
                connection.stats.circuit_state = "open"
                stats.pop(endpoint, None)
                continue

        if last_error:
            raise PageAcquireError(f"Failed to acquire page: {last_error}") from last_error
        raise PageAcquireError("Failed to acquire page: no available endpoints")

    async def release_page(self, page_wrapper: PageWrapper) -> None:
        """Release a page back to the pool.

        Args:
            page_wrapper: The PageWrapper to release.
        """
        if page_wrapper.released:
            return

        endpoint = page_wrapper.context.endpoint
        connection = self._connections.get(endpoint)
        if not connection:
            return

        await connection.release_page(page_wrapper)

    @contextmanager
    def page(self):
        """Context manager for acquiring and releasing pages.

        Yields:
            Page instance.

        Example:
            >>> with pool.page() as page:
            ...     await page.goto('https://example.com')
        """
        page_wrapper = self.acquire_page()
        try:
            yield page_wrapper.page
        finally:
            asyncio.create_task(self.release_page(page_wrapper))

    def get_stats(self) -> Dict[str, ConnectionStats]:
        """Get current statistics for all endpoints.

        Returns:
            Dictionary mapping endpoint URLs to ConnectionStats.
        """
        return {endpoint: connection.stats for endpoint, connection in self._connections.items()}

    def get_health(self) -> Dict[str, bool]:
        """Get health status for all endpoints.

        Returns:
            Dictionary mapping endpoint URLs to health status.
        """
        return {
            endpoint: connection.is_healthy for endpoint, connection in self._connections.items()
        }

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

    async def _warmup_idle_pages(self) -> None:
        """Warmup all browser connections by creating initial idle pages."""
        if self.config.min_active_page <= 0:
            return
        for connection in self._connections.values():
            await connection.warmup()

    async def _monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            await self._refresh_endpoints()
            await self._rotate_all_expiring_contexts()
            await self._refresh_connection_stats()

    async def _rotate_all_expiring_contexts(self) -> None:
        """Rotate expiring contexts for all browser connections."""
        for connection in self._connections.values():
            if connection.endpoint not in self._draining_endpoints:
                await connection.rotate_expiring_contexts()

    def _run_background(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)

        def _done_callback(done: asyncio.Task) -> None:
            try:
                done.result()
            except Exception as exc:  # noqa: BLE001
                logging.warning("background task error: %s", exc)

        task.add_done_callback(_done_callback)

    async def _refresh_connection_stats(self) -> None:
        if not self._connections:
            return
        tasks = [
            self._update_connection_stats(endpoint, connection)
            for endpoint, connection in self._connections.items()
        ]
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logging.debug("stats refresh error: %s", result)

    def _build_stats_url(self, endpoint: str) -> str:
        parsed = urlparse(endpoint)
        scheme = parsed.scheme
        if scheme == "ws":
            scheme = "http"
        elif scheme == "wss":
            scheme = "https"
        return urlunparse(parsed._replace(scheme=scheme, path="/stats", query="", fragment=""))

    async def _update_connection_stats(self, endpoint: str, connection: BrowserWrapper) -> None:
        url = self._build_stats_url(endpoint)
        payload = await asyncio.to_thread(self._fetch_stats_payload, url)
        if not payload:
            return
        stats = connection.stats
        stats.stats_cpu_percent = payload.get("cpu_percent", stats.stats_cpu_percent)
        stats.stats_mem_total_bytes = payload.get("mem_total_bytes", stats.stats_mem_total_bytes)
        stats.stats_mem_used_bytes = payload.get("mem_used_bytes", stats.stats_mem_used_bytes)
        stats.stats_mem_used_percent = payload.get("mem_used_percent", stats.stats_mem_used_percent)
        pages = payload.get("pages")
        if pages is None:
            pages = payload.get("page_count")
        if pages is not None:
            try:
                stats.stats_pages = int(pages)
            except (TypeError, ValueError):
                pass
        contexts = payload.get("contexts")
        if contexts is None:
            contexts = payload.get("context_count")
        if contexts is not None:
            try:
                stats.stats_contexts = int(contexts)
            except (TypeError, ValueError):
                pass
        if "collected_at" in payload:
            stats.stats_collected_at = str(payload.get("collected_at"))

    def _fetch_stats_payload(self, url: str) -> dict | None:
        try:
            with urlopen(url, timeout=2) as response:
                data = response.read()
        except Exception:
            return None
        try:
            return json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            return None

    async def _cleanup_endpoint(self, endpoint: str):
        """Close all contexts and disconnect connection for an endpoint."""
        connection = self._connections.pop(endpoint, None)
        if connection:
            await connection.cleanup()
            await connection.disconnect()
        self._draining_endpoints.discard(endpoint)

    async def _handle_endpoint_disconnect(self, endpoint: str) -> None:
        """Mark endpoint unhealthy and clear cached contexts/pages on disconnect."""
        connection = self._connections.get(endpoint)
        if connection:
            await connection.handle_disconnect()

    async def _drain_or_cleanup_endpoint(self, endpoint: str) -> None:
        connection = self._connections.get(endpoint)
        if not connection:
            return
        if connection.stats.active_pages > 0:
            self._draining_endpoints.add(endpoint)
            logging.info(
                "endpoint draining: %s (active_pages=%d)",
                endpoint,
                connection.stats.active_pages,
            )
            connection.stats.is_healthy = False
            connection.stats.circuit_state = "open"
            connection.stats.last_error = "draining"
            return
        await self._cleanup_endpoint(endpoint)
