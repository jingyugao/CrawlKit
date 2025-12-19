"""Context pool management with lifecycle and TTL support."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from playwright.async_api import BrowserContext

from .config import PoolConfig
from .exceptions import ContextAcquireError


class ContextWrapper:
    """Wrapper around BrowserContext with lifecycle management.

    Tracks context creation time, last use time, and availability for TTL
    and idle timeout management.

    Args:
        context: The Playwright BrowserContext instance.
        created_at: When the context was created.
    """

    def __init__(self, context: BrowserContext, created_at: datetime):
        self.context = context
        self.created_at = created_at
        self.last_used = created_at
        self.page_count = 0
        self.is_available = True
        self._lock = asyncio.Lock()

    async def acquire(self) -> BrowserContext:
        """Mark context as in use.

        Returns:
            The BrowserContext instance.

        Raises:
            ContextAcquireError: If context is not available.
        """
        async with self._lock:
            if not self.is_available:
                raise ContextAcquireError("Context is not available")

            self.is_available = False
            self.last_used = datetime.now()
            return self.context

    async def release(self):
        """Mark context as available for reuse."""
        async with self._lock:
            self.is_available = True
            self.last_used = datetime.now()

    def is_idle_timeout(self, timeout_seconds: float) -> bool:
        """Check if context has been idle too long.

        Args:
            timeout_seconds: Idle timeout in seconds.

        Returns:
            True if context has been idle longer than timeout.
        """
        idle_time = (datetime.now() - self.last_used).total_seconds()
        return idle_time > timeout_seconds

    def is_ttl_expired(self, ttl_seconds: float | None) -> bool:
        """Check if context has exceeded its TTL.

        Args:
            ttl_seconds: TTL in seconds, or None for no limit.

        Returns:
            True if TTL is exceeded.
        """
        if ttl_seconds is None:
            return False

        age = (datetime.now() - self.created_at).total_seconds()
        return age > ttl_seconds

    async def close(self):
        """Close the context."""
        try:
            await self.context.close()
        except Exception:
            pass  # Best effort cleanup

    def __repr__(self) -> str:
        age = (datetime.now() - self.created_at).total_seconds()
        return f"<ContextWrapper available={self.is_available} age={age:.1f}s>"


class ContextPool:
    """Pool of BrowserContexts for a single Browser connection.

    Manages context lifecycle, reuse, TTL, and idle cleanup.

    Args:
        browser_connection: The BrowserConnection this pool belongs to.
        config: Pool configuration.
    """

    def __init__(self, browser_connection, config: PoolConfig):
        from .wrappers import BrowserWrapper
        self.browser_connection: BrowserWrapper = browser_connection
        self.config = config

        self._contexts: set[ContextWrapper] = set()
        self._available_contexts: asyncio.Queue = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None

    async def start(self):
        """Start background cleanup task."""
        if self.config.auto_cleanup:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self):
        """Stop and cleanup all contexts."""
        # Cancel cleanup task
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # Close all contexts
        async with self._lock:
            for ctx_wrapper in list(self._contexts):
                await ctx_wrapper.close()
            self._contexts.clear()

            # Clear the queue
            while not self._available_contexts.empty():
                try:
                    self._available_contexts.get_nowait()
                except asyncio.QueueEmpty:
                    break

    async def acquire_context(self) -> BrowserContext:
        """Acquire a context from the pool or create a new one.

        Returns:
            BrowserContext instance.

        Raises:
            ContextAcquireError: If unable to acquire a context.
        """
        # Try to get an available context from queue (non-blocking)
        if self.config.reuse_contexts:
            try:
                ctx_wrapper = self._available_contexts.get_nowait()

                # Check if TTL expired
                if ctx_wrapper.is_ttl_expired(self.config.context_ttl):
                    await self._remove_context(ctx_wrapper)
                elif ctx_wrapper in self._contexts:
                    try:
                        return await ctx_wrapper.acquire()
                    except Exception:
                        # Context became invalid, remove it
                        await self._remove_context(ctx_wrapper)

            except asyncio.QueueEmpty:
                pass

        # Create new context if under limit
        async with self._lock:
            return await self._create_new_context()

        # Wait for an available context (with timeout)
        try:
            ctx_wrapper = await asyncio.wait_for(
                self._available_contexts.get(),
                timeout=self.config.acquire_timeout
            )

            # Check TTL before returning
            if ctx_wrapper.is_ttl_expired(self.config.context_ttl):
                await self._remove_context(ctx_wrapper)
                # Recursively try again
                return await self.acquire_context()

            return await ctx_wrapper.acquire()

        except asyncio.TimeoutError:
            raise ContextAcquireError(
                f"Timeout acquiring context after {self.config.acquire_timeout}s"
            )

    async def release_context(self, context: BrowserContext):
        """Release a context back to the pool.

        Args:
            context: The BrowserContext to release.
        """
        return await self.release_context_or_dispose(context, dispose=False)

    async def release_context_or_dispose(self, context: BrowserContext, dispose: bool):
        """Release a context or dispose it based on flag and TTL."""
        ctx_wrapper = self._find_wrapper_for_context(context)
        if not ctx_wrapper:
            return  # Context not managed by this pool

        if self.config.reuse_contexts:
            # Check if TTL expired before reusing
            if dispose or ctx_wrapper.is_ttl_expired(self.config.context_ttl):
                await self._remove_context(ctx_wrapper)
            else:
                await ctx_wrapper.release()
                await self._available_contexts.put(ctx_wrapper)
        else:
            # Don't reuse, just close
            await self._remove_context(ctx_wrapper)

    async def _create_new_context(self) -> BrowserContext:
        """Create a new browser context.

        Returns:
            BrowserContext instance.

        Raises:
            ContextAcquireError: If creation fails.
        """
        try:
            browser_wrapper = await self.browser_connection.connect()
            if browser_wrapper.obj is None:
                raise ContextAcquireError("Browser connection did not return an active browser")

            # Use custom context factory if provided
            if self.config.context_factory:
                context = await self.config.context_factory(browser_wrapper.obj)
            else:
                context = await browser_wrapper.obj.new_context()

            # Wrap and track
            ctx_wrapper = ContextWrapper(context, datetime.now())
            self._contexts.add(ctx_wrapper)

            # Update stats
            self.browser_connection.stats.total_contexts += 1
            self.browser_connection.stats.active_contexts = len(self._contexts)

            await ctx_wrapper.acquire()  # Mark as in use
            return context

        except Exception as e:
            raise ContextAcquireError(f"Failed to create context: {e}") from e

    async def _remove_context(self, ctx_wrapper: ContextWrapper):
        """Remove and close a context.

        Args:
            ctx_wrapper: The ContextWrapper to remove.
        """
        async with self._lock:
            if ctx_wrapper in self._contexts:
                self._contexts.remove(ctx_wrapper)
                await ctx_wrapper.close()

                # Update stats
                self.browser_connection.stats.active_contexts = len(self._contexts)

    async def _cleanup_loop(self):
        """Background task to cleanup idle and expired contexts."""
        while True:
            try:
                await asyncio.sleep(self.config.health_check_interval)
                await self._cleanup_idle_contexts()
            except asyncio.CancelledError:
                break
            except Exception:
                pass  # Continue cleanup loop

    async def _cleanup_idle_contexts(self):
        """Remove contexts that have been idle too long or exceeded TTL."""
        async with self._lock:
            to_remove = []

            for ctx_wrapper in self._contexts:
                if ctx_wrapper.is_available and (
                    ctx_wrapper.is_idle_timeout(self.config.idle_timeout) or
                    ctx_wrapper.is_ttl_expired(self.config.context_ttl)
                ):
                    to_remove.append(ctx_wrapper)

            for ctx_wrapper in to_remove:
                # Remove from set and close
                if ctx_wrapper in self._contexts:
                    self._contexts.remove(ctx_wrapper)
                    await ctx_wrapper.close()

            # Update stats if any were removed
            if to_remove:
                self.browser_connection.stats.active_contexts = len(self._contexts)

    def owns_context(self, context: BrowserContext) -> bool:
        """Check if this pool manages the given context."""
        return self._find_wrapper_for_context(context) is not None

    def _find_wrapper_for_context(self, context: BrowserContext) -> ContextWrapper | None:
        """Return the wrapper associated with the context if present."""
        for wrapper in self._contexts:
            if wrapper.context == context:
                return wrapper
        return None

    def __repr__(self) -> str:
        return f"<ContextPool contexts={len(self._contexts)} endpoint={self.browser_connection.endpoint}>"
