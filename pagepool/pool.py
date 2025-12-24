"""Main page pool implementation for high-concurrency web scraping."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
import json
import logging
from datetime import datetime
from typing import Dict, Tuple, cast, Callable, Awaitable
from urllib.parse import urlparse, urlunparse
from urllib.request import urlopen
from dataclasses import dataclass
from contextlib import asynccontextmanager
from playwright.async_api import (
    Playwright,
    BrowserContext,
    Page,
)

from .config import PoolConfig, ConnectionStats
from .wrappers import BrowserWrapper, ContextWrapper, PageWrapper
from .load_balancer import LoadBalancer
from .exceptions import PageAcquireError, NoHealthyEndpointsError


@dataclass(frozen=True)
class _PoolEvent:
    kind: str
    endpoint: str | None = None
    payload: object | None = None
    scene: str | None = None


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
        self._endpoint_contexts: Dict[str, list[ContextWrapper]] = {}
        self._idle_pages: Dict[str, asyncio.Queue[PageWrapper]] = {}
        self._load_balancer: LoadBalancer | None = None
        self._draining_endpoints: set[str] = set()
        self._event_queue: asyncio.Queue["_PoolEvent"] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._refill_pending: set[str] = set()
        self._stats_task: asyncio.Task | None = None

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

            # Start worker loop
            self._worker_task = asyncio.create_task(self._worker_loop())
            self._stats_task = asyncio.create_task(self._stats_loop())
            await self._warmup_idle_pages()

            self._started = True

    async def stop(self):
        """Shutdown the pool and cleanup all resources."""
        async with self._lock:
            if not self._started:
                return
            await self._enqueue_event(_PoolEvent(kind="stop"))
            if self._worker_task:
                await self._worker_task
                self._worker_task = None
            if self._stats_task:
                self._stats_task.cancel()
                try:
                    await self._stats_task
                except asyncio.CancelledError:
                    pass
                self._stats_task = None

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
            self._endpoint_contexts[endpoint] = []
            self._idle_pages[endpoint] = asyncio.Queue()
            self._try_fill_page(endpoint)

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

    async def acquire_page(self, scene: str | None = None) -> PageWrapper:
        """Acquire a page from the pool.

        This method:
        1. Selects an endpoint using the load balancer
        2. Gets a page from the idle queue
        3. Schedules background refill when below target
        4. Returns a PageWrapper for lifecycle management

        Returns:
            PageWrapper containing the Page object.

        Raises:
            PageAcquireError: If unable to acquire a page.
        """
        if not self._started:
            raise PageAcquireError("Pool not started; call start() first")

        # Select endpoint using load balancer, retrying other endpoints if one closes mid-acquire.
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
                page_wrapper = await self._acquire_page_for_endpoint(endpoint, connection, scene)
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
        if self.config.min_active_page <= 0:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10.0
        for endpoint in list(self._connections.keys()):
            idle_queue = self._idle_pages.get(endpoint)
            if idle_queue is None:
                continue
            target = self.config.min_active_page
            if self.config.max_idle_pages:
                target = min(target, self.config.max_idle_pages)
            logging.info("warmup start %s: idle=%d target=%d", endpoint, idle_queue.qsize(), target)
            while idle_queue.qsize() < target and loop.time() < deadline:
                await self._refill_pending_pages(endpoint)
                await asyncio.sleep(0.1)
            if idle_queue.qsize() < target:
                logging.debug(
                    "warmup incomplete for %s: idle=%d target=%d",
                    endpoint,
                    idle_queue.qsize(),
                    target,
                )
            else:
                logging.info("warmup complete %s: idle=%d", endpoint, idle_queue.qsize())

    async def _stats_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            await self._refresh_connection_stats()

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

    async def _acquire_page_for_endpoint(
        self,
        endpoint: str,
        connection: BrowserWrapper,
        scene: str | None = None,
    ) -> PageWrapper:
        """Get a page for the given endpoint from the idle queue only."""
        idle_queue = self._idle_pages[endpoint]

        # 1) Try reusing a warm idle page (not previously used by a client).
        page_wrapper = await self._pop_usable_idle_page(idle_queue, endpoint, connection)
        self._try_fill_page(endpoint)
        if page_wrapper:
            return page_wrapper
        raise PageAcquireError("No idle pages available; background refill scheduled")

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

            if await self._check_page(
                page_wrapper, endpoint, connection, mutate_stats=False
            ):
                return page_wrapper

        return None

    async def _check_page(
        self,
        page_wrapper: PageWrapper,
        endpoint: str,
        connection: BrowserWrapper,
        mutate_stats: bool = True,
    ) -> PageWrapper | None:
        """Validate an idle page and mark it as in-use."""
        ctx_wrapper = page_wrapper.context
        if ctx_wrapper.ttl_expired(self.config.context_ttl) and not ctx_wrapper.draining:
            await self._rotate_expired_contexts(endpoint, connection, [ctx_wrapper])
        if (
            page_wrapper.obj.is_closed()
            or ctx_wrapper.draining
            or ctx_wrapper.ttl_expired(self.config.context_ttl)
        ):
            await self._enqueue_event(_PoolEvent(kind="discard_page", payload=page_wrapper.obj))
            if ctx_wrapper.active_pages == 0 and (
                ctx_wrapper.draining or ctx_wrapper.ttl_expired(self.config.context_ttl)
            ):
                await self._enqueue_event(
                    _PoolEvent(kind="discard_context", endpoint=endpoint, payload=ctx_wrapper)
                )
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
        if ctx_wrapper.ttl_expired(self.config.context_ttl) and not ctx_wrapper.draining:
            await self._rotate_expired_contexts(endpoint, connection, [ctx_wrapper])
        if ctx_wrapper.draining and ctx_wrapper.active_pages == 0:
            await self._enqueue_event(
                _PoolEvent(kind="discard_context", endpoint=endpoint, payload=ctx_wrapper)
            )

        # Discard closed or expired pages
        context_expired = ctx_wrapper.ttl_expired(self.config.context_ttl)
        if page_wrapper.obj.is_closed() or context_expired:
            await self._enqueue_event(_PoolEvent(kind="discard_page", payload=page_wrapper.obj))
            if context_expired and ctx_wrapper.active_pages == 0:
                await self._enqueue_event(
                    _PoolEvent(kind="discard_context", endpoint=endpoint, payload=ctx_wrapper)
                )
            return

        # If endpoint is draining, discard and finalize cleanup when empty
        if endpoint in self._draining_endpoints:
            await self._enqueue_event(_PoolEvent(kind="discard_page", payload=page_wrapper.obj))
            if connection.stats.active_pages == 0:
                await self._enqueue_event(_PoolEvent(kind="cleanup_endpoint", endpoint=endpoint))
            return

        # No page reuse; discard after use and refill idle queue if needed.
        await self._enqueue_event(_PoolEvent(kind="discard_page", payload=page_wrapper.obj))
        if context_expired and ctx_wrapper.active_pages == 0:
            await self._enqueue_event(
                _PoolEvent(kind="discard_context", endpoint=endpoint, payload=ctx_wrapper)
            )
        self._try_fill_page(endpoint)

    def _select_context_with_capacity(
        self, contexts: list[ContextWrapper]
    ) -> ContextWrapper | None:
        """Pick the first context with remaining page capacity."""
        for record in contexts:
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

    def _can_create_more_pages(self, endpoint: str) -> bool:
        """Check against global page caps."""
        if endpoint in self._draining_endpoints:
            return False
        total_pages = sum(
            connection.stats.active_pages + connection.stats.total_pages
            for connection in self._connections.values()
        )
        if self.config.max_total_pages and total_pages >= self.config.max_total_pages:
            return False

        idle_queue = self._idle_pages.get(endpoint)
        if (
            self.config.max_idle_pages
            and idle_queue
            and idle_queue.qsize() >= self.config.max_idle_pages
        ):
            return False

        return True

    async def _create_context(self, endpoint: str, connection: BrowserWrapper) -> ContextWrapper:
        """Create and register a new context for an endpoint."""
        try:
            browser_wrapper = await connection.connect()
        except Exception as exc:
            logging.warning("connect failed for %s: %s", endpoint, exc)
            raise
        if browser_wrapper.obj is None:
            raise PageAcquireError("Browser connection did not return an active browser")
        try:
            if self.config.context_factory:
                context = await self.config.context_factory(browser_wrapper.obj)
            else:
                context = await browser_wrapper.obj.new_context()
        except Exception as exc:
            logging.warning("create context failed for %s: %s", endpoint, exc)
            raise

        record = ContextWrapper(obj=context, endpoint=endpoint, created_at=datetime.now())
        self._endpoint_contexts[endpoint].append(record)

        connection.stats.total_contexts += 1
        connection.stats.active_contexts = len(self._endpoint_contexts[endpoint])
        return record

    def _resolve_page_init(self, scene: str | None) -> Callable[[Page], Awaitable[None]] | None:
        """Select page init by scene if mapping is provided."""
        initializer = self.config.page_init
        if initializer is None:
            return None
        if isinstance(initializer, dict):
            if scene is None:
                return initializer.get("default") or next(iter(initializer.values()), None)
            return initializer.get(scene)
        return initializer

    async def _apply_page_init(self, page: Page, scene: str | None) -> None:
        initializer = self._resolve_page_init(scene)
        if initializer:
            await initializer(page)

    async def _spawn_idle_page(self, endpoint: str):
        """Create a page and return it to the idle queue without counting as active."""
        if self.config.min_active_page <= 0:
            return
        if endpoint in self._draining_endpoints:
            return
        if endpoint not in self._connections:
            return

        connection = self._connections[endpoint]
        contexts = self._endpoint_contexts[endpoint]

        await self._rotate_expired_contexts(endpoint, connection)
        context_with_capacity = self._select_context_with_capacity(contexts)
        if not context_with_capacity:
            if not self._can_create_more_pages(endpoint):
                return
            try:
                context_with_capacity = await self._create_context(endpoint, connection)
            except Exception as exc:
                logging.warning("spawn: create context failed for %s: %s", endpoint, exc)
                return

        if context_with_capacity.draining or context_with_capacity.ttl_expired(
            self.config.context_ttl
        ):
            return

        try:
            page = await context_with_capacity.obj.new_page()
        except Exception as exc:
            logging.warning("spawn: new page failed for %s: %s", endpoint, exc)
            return

        try:
            await self._apply_page_init(page, None)
        except Exception as exc:
            logging.warning("spawn: page init failed for %s: %s", endpoint, exc)
            await self._discard_page(page)
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
        if endpoint in self._draining_endpoints:
            return
        idle_queue = self._idle_pages.get(endpoint)
        if idle_queue is None:
            return

        target = self.config.min_active_page
        if self.config.max_idle_pages:
            target = min(target, self.config.max_idle_pages)
        deficit = target - idle_queue.qsize()
        if deficit <= 0:
            return
        if endpoint in self._refill_pending:
            return
        self._refill_pending.add(endpoint)
        try:
            self._event_queue.put_nowait(_PoolEvent(kind="refill", endpoint=endpoint))
        except asyncio.QueueFull:
            self._refill_pending.discard(endpoint)
            logging.warning("pool event queue full: refill")

    def _try_fill_page(self, endpoint: str) -> None:
        """Try scheduling a refill when it makes sense for the endpoint."""
        if self.config.min_active_page <= 0:
            return
        if endpoint in self._draining_endpoints:
            return
        if endpoint not in self._connections:
            return
        if not self._can_create_more_pages(endpoint):
            return
        self._schedule_spawn_pages(endpoint)

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
                    if wrapper.context == ctx_record:
                        await self._discard_page(wrapper.obj)
                    else:
                        await remaining.put(wrapper)
                self._idle_pages[endpoint] = remaining

    async def _purge_idle_pages_for_context(
        self, endpoint: str, ctx_record: ContextWrapper
    ) -> None:
        idle_queue = self._idle_pages.get(endpoint)
        if not idle_queue:
            return
        remaining: asyncio.Queue[PageWrapper] = asyncio.Queue()
        while not idle_queue.empty():
            try:
                wrapper = idle_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if wrapper.context == ctx_record:
                await self._discard_page(wrapper.obj)
            else:
                await remaining.put(wrapper)
        self._idle_pages[endpoint] = remaining

    async def _rotate_expired_contexts(
        self,
        endpoint: str,
        connection: BrowserWrapper,
        contexts: list[ContextWrapper] | None = None,
    ) -> None:
        if self.config.context_ttl is None:
            return
        if endpoint in self._draining_endpoints:
            return
        if contexts is None:
            contexts = list(self._endpoint_contexts.get(endpoint, []))
        for record in contexts:
            if record.draining or not record.ttl_expired(self.config.context_ttl):
                continue
            try:
                await self._create_context(endpoint, connection)
            except Exception:
                continue
            record.draining = True
            await self._purge_idle_pages_for_context(endpoint, record)
            if record.active_pages == 0:
                await self._enqueue_event(
                    _PoolEvent(kind="discard_context", endpoint=endpoint, payload=record)
                )

    async def _enqueue_event(self, event: _PoolEvent) -> None:
        try:
            self._event_queue.put_nowait(event)
        except asyncio.QueueFull:
            logging.warning("pool event queue full: %s", event.kind)

    async def _worker_loop(self) -> None:
        while True:
            event = await self._event_queue.get()
            if event.kind == "stop":
                break
            if event.kind == "refill":
                if event.endpoint:
                    await self._refill_pending_pages(event.endpoint)
                continue
            if event.kind == "discard_page" and event.payload:
                await self._discard_page(cast(Page, event.payload))
                continue
            if event.kind == "discard_context" and event.payload and event.endpoint:
                await self._discard_context(cast(ContextWrapper, event.payload), event.endpoint)
                continue
            if event.kind == "cleanup_endpoint" and event.endpoint:
                await self._cleanup_endpoint(event.endpoint)
                continue
            if event.kind == "disconnect" and event.endpoint:
                await self._handle_disconnect_event(event.endpoint)
                continue

    async def _refill_pending_pages(self, endpoint: str) -> None:
        try:
            idle_queue = self._idle_pages.get(endpoint)
            if idle_queue is None:
                return
            target = self.config.min_active_page
            if self.config.max_idle_pages:
                target = min(target, self.config.max_idle_pages)
            deficit = target - idle_queue.qsize()
            if deficit <= 0:
                return
            logging.info("refill %s: deficit=%d", endpoint, deficit)
            for _ in range(deficit):
                await self._spawn_idle_page(endpoint)
        finally:
            self._refill_pending.discard(endpoint)

    async def _handle_disconnect_event(self, endpoint: str) -> None:
        connection = self._connections.get(endpoint)
        if not connection:
            return
        logging.warning("endpoint disconnected: %s", endpoint)
        connection.stats.is_healthy = False
        connection.stats.circuit_state = "open"
        connection.stats.last_error = "cdp disconnected"
        connection.stats.active_connections = 0
        connection.stats.active_pages = 0

        contexts = self._endpoint_contexts.get(endpoint, [])
        for record in list(contexts):
            await self._discard_context(record, endpoint)

        idle_queue = self._idle_pages.get(endpoint)
        if idle_queue:
            while not idle_queue.empty():
                try:
                    wrapper = idle_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                await self._discard_page(wrapper.obj)

    def _find_context_record(
        self, context: BrowserContext | ContextWrapper
    ) -> Tuple[str | None, ContextWrapper | None]:
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
        logging.info("endpoint cleanup: %s", endpoint)
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
        self._draining_endpoints.discard(endpoint)

    async def _handle_endpoint_disconnect(self, endpoint: str) -> None:
        """Mark endpoint unhealthy and clear cached contexts/pages on disconnect."""
        await self._enqueue_event(_PoolEvent(kind="disconnect", endpoint=endpoint))

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
        await self._enqueue_event(_PoolEvent(kind="cleanup_endpoint", endpoint=endpoint))
