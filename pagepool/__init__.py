"""pagepool - Playwright Page Pool for high-concurrency web scraping.

A high-performance page pool implementation with CDP protocol support,
dynamic endpoint management, custom load balancing, and TTL management.
"""

from .pool import PlaywrightPagePool
from .config import PoolConfig, ConnectionStats
from .wrappers import PageWrapper
from .load_balancer import (
    LoadBalancer,
    round_robin_balancer,
    random_balancer,
    weighted_balancer,
    least_connections_balancer,
)
from .exceptions import (
    PoolException,
    NoHealthyEndpointsError,
    EndpointConnectionError,
    ContextAcquireError,
    PageAcquireError,
)

__version__ = "0.1.0"

__all__ = [
    # Main classes
    "PlaywrightPagePool",
    "PoolConfig",
    "ConnectionStats",
    "PageWrapper",
    "LoadBalancer",
    # Load balancing strategies
    "round_robin_balancer",
    "random_balancer",
    "weighted_balancer",
    "least_connections_balancer",
    # Exceptions
    "PoolException",
    "NoHealthyEndpointsError",
    "EndpointConnectionError",
    "ContextAcquireError",
    "PageAcquireError",
]
