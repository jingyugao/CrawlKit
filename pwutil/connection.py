"""CDP connection management with circuit breaker pattern."""

import asyncio
from typing import Optional
from enum import Enum
from datetime import datetime, timedelta
from playwright.async_api import Playwright, Browser

from .config import PoolConfig, ConnectionStats
from .exceptions import CircuitBreakerOpenError, EndpointConnectionError


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"        # Normal operation
    OPEN = "open"            # Circuit is open, failing fast
    HALF_OPEN = "half_open"  # Testing if service recovered


class CircuitBreaker:
    """Circuit breaker to prevent cascading failures.

    The circuit breaker has three states:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Too many failures, requests fail immediately
    - HALF_OPEN: Testing if service recovered

    Args:
        failure_threshold: Number of failures before opening circuit.
        recovery_timeout: Time to wait before trying half-open state (seconds).
        half_open_max_calls: Number of successful calls to close circuit from half-open.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        half_open_max_calls: int = 3
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls

        self.failure_count = 0
        self.success_count = 0
        self.state = CircuitState.CLOSED
        self.last_failure_time: Optional[datetime] = None
        self.half_open_calls = 0
        self._lock = asyncio.Lock()

    async def call(self, func, *args, **kwargs):
        """Execute a function with circuit breaker protection.

        Args:
            func: Async function to execute.
            *args: Positional arguments for the function.
            **kwargs: Keyword arguments for the function.

        Returns:
            Result of the function call.

        Raises:
            CircuitBreakerOpenError: If circuit is open.
            Exception: Any exception raised by the function.
        """
        # Check if we should transition from OPEN to HALF_OPEN
        async with self._lock:
            if self.state == CircuitState.OPEN:
                if self.last_failure_time and datetime.now() - self.last_failure_time > timedelta(seconds=self.recovery_timeout):
                    self.state = CircuitState.HALF_OPEN
                    self.half_open_calls = 0
                else:
                    raise CircuitBreakerOpenError(
                        f"Circuit breaker is OPEN. Will retry after {self.recovery_timeout}s"
                    )

            if self.state == CircuitState.HALF_OPEN:
                if self.half_open_calls >= self.half_open_max_calls:
                    raise CircuitBreakerOpenError(
                        f"Circuit breaker is HALF_OPEN (max test calls reached)"
                    )

        # Execute the function
        try:
            result = await func(*args, **kwargs)
            await self._on_success()
            return result
        except Exception as e:
            await self._on_failure()
            raise

    async def _on_success(self):
        """Handle successful call."""
        async with self._lock:
            self.success_count += 1
            self.failure_count = 0

            if self.state == CircuitState.HALF_OPEN:
                self.half_open_calls += 1
                if self.half_open_calls >= self.half_open_max_calls:
                    # Successfully recovered, close the circuit
                    self.state = CircuitState.CLOSED
                    self.half_open_calls = 0

    async def _on_failure(self):
        """Handle failed call."""
        async with self._lock:
            self.failure_count += 1
            self.last_failure_time = datetime.now()

            if self.state == CircuitState.HALF_OPEN:
                # Failed during recovery, reopen circuit
                self.state = CircuitState.OPEN
            elif self.failure_count >= self.failure_threshold:
                # Too many failures, open the circuit
                self.state = CircuitState.OPEN

    def is_available(self) -> bool:
        """Check if circuit is available (not open)."""
        return self.state != CircuitState.OPEN

    @property
    def state_name(self) -> str:
        """Get current state as string."""
        return self.state.value


class BrowserConnection:
    """Manages a single CDP connection to a remote Chrome instance.

    Handles connection lifecycle, health checking, and circuit breaker pattern.

    Args:
        endpoint: CDP endpoint URL (e.g., 'http://localhost:9222').
        playwright: Playwright instance.
        config: Pool configuration.
    """

    def __init__(
        self,
        endpoint: str,
        playwright: Playwright,
        config: PoolConfig
    ):
        self.endpoint = endpoint
        self.playwright = playwright
        self.config = config

        self.browser: Optional[Browser] = None
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=config.failure_threshold,
            recovery_timeout=config.recovery_timeout,
            half_open_max_calls=config.half_open_max_calls
        )

        self.stats = ConnectionStats(endpoint=endpoint)
        self._connection_lock = asyncio.Lock()
        self._is_connected = False
        self._last_health_check: Optional[datetime] = None

    async def connect(self) -> Browser:
        """Establish CDP connection to remote Chrome.

        Returns:
            Browser instance.

        Raises:
            EndpointConnectionError: If connection fails.
            CircuitBreakerOpenError: If circuit breaker is open.
        """
        async with self._connection_lock:
            # Return existing connection if still valid
            if self._is_connected and self.browser:
                try:
                    # Quick check if browser is still responsive
                    _ = self.browser.contexts
                    return self.browser
                except Exception:
                    # Connection is stale, will reconnect
                    self._is_connected = False
                    self.browser = None

            # Connect via circuit breaker
            try:
                self.browser = await self.circuit_breaker.call(
                    self.playwright.chromium.connect_over_cdp,
                    self.endpoint,
                    timeout=self.config.connection_timeout * 1000,  # Convert to ms
                    headers=self.config.cdp_headers,
                    slow_mo=self.config.slow_mo
                )

                self._is_connected = True
                self.stats.is_healthy = True
                self.stats.total_connections += 1
                self.stats.active_connections = 1
                self.stats.circuit_state = self.circuit_breaker.state_name
                self.stats.success_count += 1

                return self.browser

            except CircuitBreakerOpenError:
                self.stats.circuit_state = self.circuit_breaker.state_name
                raise
            except Exception as e:
                self.stats.is_healthy = False
                self.stats.last_error = str(e)
                self.stats.error_count += 1
                self.stats.circuit_state = self.circuit_breaker.state_name

                raise EndpointConnectionError(
                    f"Failed to connect to {self.endpoint}: {e}"
                ) from e

    async def disconnect(self):
        """Close the browser connection."""
        async with self._connection_lock:
            if self.browser:
                try:
                    await self.browser.close()
                except Exception:
                    pass  # Best effort cleanup
                finally:
                    self.browser = None
                    self._is_connected = False
                    self.stats.active_connections = 0

    async def health_check(self) -> bool:
        """Perform health check on the connection.

        Returns:
            True if healthy, False otherwise.
        """
        try:
            if not self._is_connected or not self.browser:
                return False

            # Try to access browser contexts as a health check
            _ = self.browser.contexts

            self.stats.is_healthy = True
            self.stats.success_count += 1
            self.stats.circuit_state = self.circuit_breaker.state_name
            self._last_health_check = datetime.now()
            return True

        except Exception as e:
            self.stats.is_healthy = False
            self.stats.last_error = str(e)
            self.stats.error_count += 1
            self.stats.circuit_state = self.circuit_breaker.state_name
            return False

    @property
    def is_healthy(self) -> bool:
        """Check if connection is healthy and available."""
        return self.stats.is_healthy and self.circuit_breaker.is_available()

    def __repr__(self) -> str:
        return f"<BrowserConnection endpoint={self.endpoint} healthy={self.is_healthy}>"
