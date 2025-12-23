"""Load balancing strategies for endpoint selection."""

from __future__ import annotations

import random
from typing import Callable, Dict

from .config import ConnectionStats
from .exceptions import NoHealthyEndpointsError


class LoadBalancer:
    """Manages load balancing across multiple CDP endpoints.

    Args:
        endpoints: List of endpoint URLs.
        custom_balancer: Optional custom load balancing function.
    """

    def __init__(
        self,
        endpoints: list[str],
        custom_balancer: Callable[[Dict[str, ConnectionStats]], str] | None = None
    ):
        self.endpoints = endpoints
        self.custom_balancer = custom_balancer

    def select_endpoint(self, stats: Dict[str, ConnectionStats]) -> str:
        """Select an endpoint based on load balancing strategy.

        Args:
            stats: Dictionary mapping endpoint URLs to ConnectionStats.

        Returns:
            Selected endpoint URL.

        Raises:
            NoHealthyEndpointsError: If no healthy endpoints are available.
        """
        if self.custom_balancer:
            return self.custom_balancer(stats)

        return self._default_least_loaded(stats)

    def _default_least_loaded(self, stats: Dict[str, ConnectionStats]) -> str:
        """Default strategy: select endpoint with lowest load score.

        Args:
            stats: Dictionary mapping endpoint URLs to ConnectionStats.

        Returns:
            Selected endpoint URL.

        Raises:
            NoHealthyEndpointsError: If no healthy endpoints are available.
        """
        # Filter to healthy endpoints
        healthy = {
            ep: s for ep, s in stats.items()
            if s.is_healthy and s.circuit_state != "open"
        }

        if not healthy:
            # Fallback: try any endpoint if none are healthy
            if stats:
                return random.choice(list(stats.keys()))
            raise NoHealthyEndpointsError("No endpoints available")

        # Select endpoint with minimum load score
        selected = min(healthy.items(), key=lambda x: x[1].load_score)
        return selected[0]


# Example custom load balancers users can provide


def round_robin_balancer(state: dict | None = None):
    """Create a round-robin load balancer.

    Args:
        state: Optional state dictionary to track current index.

    Returns:
        Load balancer function.

    Example:
        >>> balancer = round_robin_balancer()
        >>> config = PoolConfig(
        ...     cdp_endpoints=['http://chrome1:9222', 'http://chrome2:9222'],
        ...     load_balancer=balancer
        ... )
    """
    if state is None:
        state = {"index": 0}

    def balancer(stats: Dict[str, ConnectionStats]) -> str:
        # Filter healthy endpoints
        healthy = [
            ep for ep, s in stats.items()
            if s.is_healthy and s.circuit_state != "open"
        ]

        if not healthy:
            raise NoHealthyEndpointsError("No healthy endpoints")

        # Round-robin selection
        endpoint = healthy[state["index"] % len(healthy)]
        state["index"] += 1
        return endpoint

    return balancer


def random_balancer(stats: Dict[str, ConnectionStats]) -> str:
    """Random selection among healthy endpoints.

    Args:
        stats: Dictionary mapping endpoint URLs to ConnectionStats.

    Returns:
        Randomly selected healthy endpoint.

    Raises:
        NoHealthyEndpointsError: If no healthy endpoints are available.

    Example:
        >>> config = PoolConfig(
        ...     cdp_endpoints=['http://chrome1:9222', 'http://chrome2:9222'],
        ...     load_balancer=random_balancer
        ... )
    """
    healthy = [
        ep for ep, s in stats.items()
        if s.is_healthy and s.circuit_state != "open"
    ]

    if not healthy:
        raise NoHealthyEndpointsError("No healthy endpoints")

    return random.choice(healthy)


def weighted_balancer(weights: Dict[str, float]):
    """Create a weighted random load balancer.

    Args:
        weights: Dictionary mapping endpoint URLs to weights.

    Returns:
        Load balancer function.

    Example:
        >>> weights = {
        ...     'http://chrome1:9222': 2.0,  # 2x weight
        ...     'http://chrome2:9222': 1.0,  # 1x weight
        ... }
        >>> balancer = weighted_balancer(weights)
        >>> config = PoolConfig(
        ...     cdp_endpoints=list(weights.keys()),
        ...     load_balancer=balancer
        ... )
    """
    def balancer(stats: Dict[str, ConnectionStats]) -> str:
        # Filter healthy endpoints with their weights
        healthy = [
            (ep, weights.get(ep, 1.0))
            for ep, s in stats.items()
            if s.is_healthy and s.circuit_state != "open"
        ]

        if not healthy:
            raise NoHealthyEndpointsError("No healthy endpoints")

        # Weighted random selection
        total_weight = sum(weight for _, weight in healthy)
        r = random.uniform(0, total_weight)

        cumsum = 0.0
        for ep, weight in healthy:
            cumsum += weight
            if r <= cumsum:
                return ep

        # Fallback to last endpoint
        return healthy[-1][0]

    return balancer


def least_connections_balancer(stats: Dict[str, ConnectionStats]) -> str:
    """Select endpoint with least active connections.

    Args:
        stats: Dictionary mapping endpoint URLs to ConnectionStats.

    Returns:
        Endpoint with least active connections.

    Raises:
        NoHealthyEndpointsError: If no healthy endpoints are available.

    Example:
        >>> config = PoolConfig(
        ...     cdp_endpoints=['http://chrome1:9222', 'http://chrome2:9222'],
        ...     load_balancer=least_connections_balancer
        ... )
    """
    healthy = {
        ep: s for ep, s in stats.items()
        if s.is_healthy and s.circuit_state != "open"
    }

    if not healthy:
        raise NoHealthyEndpointsError("No healthy endpoints")

    # Select endpoint with minimum active pages
    selected = min(healthy.items(), key=lambda x: x[1].active_pages)
    return selected[0]
