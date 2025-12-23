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

    def __post_init__(self):
        self._lock = asyncio.Lock()

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


@dataclass
class ContextWrapper:
    """Wraps a Playwright BrowserContext instance with metadata."""

    obj: BrowserContext
    endpoint: str
    created_at: datetime
    active_pages: int = 0

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
