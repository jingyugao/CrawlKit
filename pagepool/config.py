"""Configuration and statistics classes for the page pool."""

from __future__ import annotations

from typing import Callable, Any, Dict, Union, Awaitable

from playwright.async_api import Browser, BrowserContext
from pydantic import BaseModel, ConfigDict, Field


class PoolConfig(BaseModel):
    """Configuration for the Playwright page pool.

    Args:
        cdp_endpoints: List of CDP endpoint URLs, or a function that returns the list.
                      Supports static list, sync function, or async function.
        endpoints_refresh_interval: How often to refresh endpoints (seconds) when using dynamic endpoints.
        max_connections_per_endpoint: Maximum browser connections per endpoint.
        max_contexts_per_connection: Maximum contexts per browser connection.
        max_pages_per_context: Maximum pages per context (currently not enforced strictly).
        connection_timeout: Timeout for establishing CDP connection (seconds).
        acquire_timeout: Timeout for acquiring a resource from pool (seconds).
        idle_timeout: How long a context can be idle before cleanup (seconds).
        page_ttl: Maximum lifetime of a page (seconds). None means no limit.
        context_ttl: Maximum lifetime of a context (seconds). None means no limit.
        health_check_interval: How often to run health checks (seconds).
        context_factory: Optional custom function to create browser contexts.
        load_balancer: Optional custom function to select endpoints for load balancing.
        cdp_headers: Optional HTTP headers to send with CDP connection.
        slow_mo: Slow down Playwright operations by this amount (milliseconds).
        reuse_contexts: Whether to reuse contexts across page acquisitions.
        auto_cleanup: Whether to automatically cleanup idle resources.
        strict_mode: If True, raise errors on connection failures. If False, retry/continue.
        failure_threshold: Number of failures before opening circuit breaker.
        recovery_timeout: Time to wait before trying to close circuit breaker (seconds).
        half_open_max_calls: Number of successful calls needed to close circuit breaker.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # CDP endpoints (static or dynamic)
    cdp_endpoints: Union[
        list[str],
        Callable[[], list[str]],
        Callable[[], Awaitable[list[str]]]
    ] = Field(...)

    # Endpoint management
    endpoints_refresh_interval: float = 60.0

    # Pool sizing
    max_connections_per_endpoint: int = 5
    max_contexts_per_connection: int = 10
    max_pages_per_context: int = 5
    min_active_page: int = 0

    # Timeouts
    connection_timeout: float = 30.0
    acquire_timeout: float = 10.0
    idle_timeout: float = 300.0  # 5 minutes

    # TTL management
    page_ttl: float | None = None
    context_ttl: float | None = None

    # Health checking
    health_check_interval: float = 60.0

    # Customization
    context_factory: Callable[[Browser], Awaitable[BrowserContext]] | None = None
    load_balancer: Callable[[Dict[str, "ConnectionStats"]], str] | None = None

    # Connection parameters
    cdp_headers: Dict[str, str] | None = None
    slow_mo: float = 0.0

    # Behavior flags
    reuse_contexts: bool = True
    auto_cleanup: bool = True
    strict_mode: bool = False

    # Circuit breaker settings (unused in current build, kept for compatibility)
    failure_threshold: int = 5
    recovery_timeout: float = 60.0
    half_open_max_calls: int = 3


class ConnectionStats(BaseModel):
    """Statistics for a single CDP endpoint connection."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    endpoint: str
    total_connections: int = 0
    active_connections: int = 0
    total_contexts: int = 0
    active_contexts: int = 0
    total_pages: int = 0
    active_pages: int = 0
    is_healthy: bool = True
    circuit_state: str = "closed"
    last_error: str | None = None
    error_count: int = 0
    success_count: int = 0

    @property
    def load_score(self) -> float:
        """Calculate a load score for this endpoint.

        Lower scores are better. Unhealthy or open circuits return infinity.
        The score is based on active pages plus a weighted contribution from contexts.

        Returns:
            Load score (lower is better).
        """
        if not self.is_healthy or self.circuit_state == "open":
            return float("inf")

        # Score = active pages + (active contexts * weight)
        # This gives preference to endpoints with fewer pages
        return self.active_pages + (self.active_contexts * 0.5)

    def to_dict(self) -> Dict[str, Any]:
        """Convert stats to dictionary."""
        data = self.model_dump()
        data["load_score"] = self.load_score
        return data
