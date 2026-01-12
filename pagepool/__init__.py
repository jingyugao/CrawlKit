"""PagePool - Simplified Playwright page pool for single browser management."""

from pagepool.pool import PagePool, PagePoolError, PoolNotStartedError
from pagepool.wrappers import ContextWrapper, PageWrapper

__version__ = "0.2.0"

__all__ = [
    "PagePool",
    "PagePoolError",
    "PoolNotStartedError",
    "ContextWrapper",
    "PageWrapper",
]
