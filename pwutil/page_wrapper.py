"""Page wrapper with lifecycle management and TTL support."""

import asyncio
from datetime import datetime
from playwright.async_api import Page, BrowserContext


class PageWrapper:
    """Wrapper around Page with lifecycle management.

    Provides context manager support and TTL checking for pages.

    Args:
        page: The Playwright Page instance.
        context: The BrowserContext this page belongs to.
        pool: The PlaywrightPagePool instance.
    """

    def __init__(self, page: Page, context: BrowserContext, pool):
        from .pool import PlaywrightPagePool
        self.page = page
        self.context = context
        self.pool: PlaywrightPagePool = pool
        self._released = False
        self.created_at = datetime.now()
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        """Context manager entry."""
        return self.page

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - auto release."""
        await self.release()

    def is_ttl_expired(self) -> bool:
        """Check if page has exceeded its TTL.

        Returns:
            True if TTL is exceeded, False otherwise.
        """
        if self.pool.config.page_ttl is None:
            return False

        age = (datetime.now() - self.created_at).total_seconds()
        return age > self.pool.config.page_ttl

    async def release(self):
        """Release the page back to the pool."""
        async with self._lock:
            if self._released:
                return

            self._released = True
            await self.pool.release_page(self)

    async def close(self):
        """Close the page and release it."""
        try:
            if not self.page.is_closed():
                await self.page.close()
        except Exception:
            pass  # Best effort cleanup
        finally:
            await self.release()

    def __repr__(self) -> str:
        age = (datetime.now() - self.created_at).total_seconds()
        return f"<PageWrapper released={self._released} age={age:.1f}s>"
