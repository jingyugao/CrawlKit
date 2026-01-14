from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class SceneConfig:
    """Configuration for a specific browser scene."""
    name: str
    user_agent: Optional[str] = None
    viewport: Optional[Dict[str, int]] = None
    proxy: Optional[Dict[str, str]] = None
    geolocation: Optional[Dict[str, float]] = None
    permissions: Optional[List[str]] = None
    extra_http_headers: Optional[Dict[str, str]] = None
    # Additional playwright context options
    context_options: Dict[str, Any] = field(default_factory=dict)

@dataclass
class EndpointConfig:
    """Configuration for a CDP endpoint."""
    url: str
    weight: int = 100
    tags: List[str] = field(default_factory=list)
    node_name: Optional[str] = None  # For node affinity
    metadata: Dict[str, Any] = field(default_factory=dict)
    # Circuit breaker settings
    failure_threshold: int = 5
    recovery_timeout: float = 30.0

@dataclass
class PoolConfig:
    """Configuration for the pool behavior."""
    min_idle: int = 2
    max_total: int = 20
    max_context_uses: int = 100
    context_ttl: float = 300.0  # seconds
    health_check_interval: float = 30.0