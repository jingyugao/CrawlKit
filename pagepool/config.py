"""Configuration and statistics classes for the page pool."""

from __future__ import annotations

from typing import Callable, Any, Dict, Awaitable

from playwright.async_api import Browser, BrowserContext, Page
from pydantic import BaseModel, ConfigDict, Field


class PoolConfig(BaseModel):
    """Configuration for the Playwright page pool.

    Args:
        cdp_endpoints: List of CDP endpoint URLs, or a function that returns the list.
                      Supports static list, sync function, or async function.
        max_pages_per_context: Maximum pages per context (soft cap).
        connection_timeout: Timeout for establishing CDP connection (seconds).
        idle_timeout: How long a context can be idle before cleanup (seconds).
        context_ttl: Maximum lifetime of a context (seconds). None means no limit.
        health_check_interval: How often to run health checks (seconds).
        context_factory: Optional custom function to create browser contexts.
        load_balancer: Optional custom function to select endpoints for load balancing.
        cdp_connect_opts: Extra kwargs passed to Playwright chromium.connect_over_cdp (headers, slow_mo, timeout, etc.).
        page_init: Optional function (or mapping of scene -> function) to initialize pages before use.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # CDP endpoints (static or dynamic)
    cdp_endpoints: list[str] | Callable[[], list[str]] | Callable[[], Awaitable[list[str]]] = Field(...)

    # Pool sizing
    max_pages_per_context: int = 0  # 0 means no cap
    max_total_pages: int = 0  # 0 means no cap
    max_idle_pages: int = 0  # 0 means no cap
    min_active_page: int = 0

    # Timeouts
    connection_timeout: float = 30.0
    idle_timeout: float = 300.0  # 5 minutes

    # TTL management
    context_ttl: float | None = None

    # Health checking
    health_check_interval: float = 60.0

    # Customization
    context_factory: Callable[[Browser], Awaitable[BrowserContext]] | None = None
    page_init: Callable[[Page], Awaitable[None]] | Dict[str, Callable[[Page], Awaitable[None]]] | None = None
    load_balancer: Callable[[Dict[str, "ConnectionStats"]], str] | None = None

    # Connection parameters
    cdp_connect_opts: Dict[str, Any] = Field(default_factory=dict)


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
    stats_cpu_percent: float | None = None
    stats_mem_total_bytes: int | None = None
    stats_mem_used_bytes: int | None = None
    stats_mem_used_percent: float | None = None
    stats_pages: int | None = None
    stats_contexts: int | None = None
    stats_collected_at: str | None = None

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
