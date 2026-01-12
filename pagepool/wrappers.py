"""Wrapper classes for Playwright objects with lifecycle management."""
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Awaitable
from playwright.async_api import BrowserContext, Page


@dataclass
class ContextWrapper:
    """Wrapper for BrowserContext with lifecycle metadata."""

    obj: BrowserContext
    created_at: datetime
    active_pages: int = 0
    draining: bool = False

    def inc_pages(self) -> None:
        """Increment active page count."""
        self.active_pages += 1

    def dec_pages(self) -> None:
        """Decrement active page count."""
        self.active_pages = max(0, self.active_pages - 1)

    def ttl_expired(self, ttl: float | None) -> bool:
        """Check if context has exceeded TTL.

        Args:
            ttl: Time-to-live in seconds, or None for no limit

        Returns:
            True if TTL exceeded, False otherwise
        """
        if ttl is None:
            return False

        age = (datetime.now() - self.created_at).total_seconds()
        return age >= ttl

    @property
    def context(self) -> BrowserContext:
        """Alias for obj for backward compatibility."""
        return self.obj


@dataclass
class PageWrapper:
    """Wrapper for Page with usage tracking and lifecycle metadata."""

    obj: Page
    context: ContextWrapper
    created_at: datetime
    use_count: int = 0
    released: bool = False
    _releaser: Callable[["PageWrapper"], Awaitable[None]] | None = None

    def increment_use(self) -> None:
        """Increment usage count."""
        self.use_count += 1

    def is_max_uses_exceeded(self, max_uses: int | None) -> bool:
        """Check if page has exceeded maximum usage count.

        Args:
            max_uses: Maximum usage count, or None for no limit

        Returns:
            True if max uses exceeded, False otherwise
        """
        if max_uses is None:
            return False
        return self.use_count >= max_uses

    def mark_released(self) -> None:
        """Mark page as released."""
        self.released = True

    async def release(self) -> None:
        """Release the page back to pool if releaser is set."""
        if self._releaser and not self.released:
            await self._releaser(self)

    @property
    def page(self) -> Page:
        """Alias for obj for backward compatibility."""
        return self.obj

    @property
    def context_obj(self) -> BrowserContext:
        """Get the underlying BrowserContext object."""
        return self.context.obj
