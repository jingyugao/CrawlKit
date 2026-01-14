import abc
import asyncio
from typing import AsyncIterator, List

from pagepool.types import EndpointConfig

class EndpointDiscovery(abc.ABC):
    """Abstract base class for endpoint discovery mechanisms."""

    @abc.abstractmethod
    async def watch(self) -> AsyncIterator[List[EndpointConfig]]:
        """Yields updated lists of endpoints."""
        pass

class StaticDiscovery(EndpointDiscovery):
    """Static list of endpoints."""

    def __init__(self, endpoints: List[EndpointConfig]):
        self.endpoints = endpoints
        self._sent = False

    async def watch(self) -> AsyncIterator[List[EndpointConfig]]:
        if not self._sent:
            self._sent = True
            yield self.endpoints
        # Keep the iterator alive but don't yield anything else
        # This simulates a watch that never updates
        while True:
            await asyncio.sleep(3600)
