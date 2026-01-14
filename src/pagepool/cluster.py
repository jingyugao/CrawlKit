import asyncio
import logging
from typing import Dict, List, Optional, Set, Union

from playwright.async_api import Playwright, async_playwright

from pagepool.discovery import EndpointDiscovery, StaticDiscovery
from pagepool.endpoint import SingleEndpointPool
from pagepool.load_balancer import LoadBalancerStrategy, RoundRobinStrategy
from pagepool.pool import PagePoolError
from pagepool.types import EndpointConfig, PoolConfig, SceneConfig
from pagepool.wrappers import PageWrapper

logger = logging.getLogger(__name__)

class ClusterPagePool:
    """Manages a cluster of CDP endpoints for high-availability page pooling."""

    def __init__(
        self, 
        endpoints: Union[List[EndpointConfig], EndpointDiscovery],
        pool_config: PoolConfig = PoolConfig(),
        strategy: LoadBalancerStrategy = RoundRobinStrategy()
    ):
        if isinstance(endpoints, list):
            self.discovery = StaticDiscovery(endpoints)
        else:
            self.discovery = endpoints
            
        self.pool_config = pool_config
        self.strategy = strategy
        
        self.scenes: Dict[str, SceneConfig] = {
            "default": SceneConfig(name="default")
        }
        
        self.playwright: Optional[Playwright] = None
        self.endpoints: List[SingleEndpointPool] = []
        self._draining_endpoints: List[SingleEndpointPool] = []
        self._started = False
        self._maintenance_task: Optional[asyncio.Task] = None
        self._watch_task: Optional[asyncio.Task] = None
        self._update_lock = asyncio.Lock()

    def define_scene(self, config: SceneConfig) -> None:
        """Register a new scene configuration."""
        self.scenes[config.name] = config

    async def start(self) -> None:
        """Start the cluster manager and connect to endpoints."""
        if self._started:
            return

        logger.info("Starting ClusterPagePool")
        self.playwright = await async_playwright().start()
        
        self._started = True
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())
        self._watch_task = asyncio.create_task(self._watch_loop())

    async def stop(self) -> None:
        """Stop the cluster and close all connections."""
        if not self._started:
            return

        logger.info("Stopping ClusterPagePool")
        self._started = False
        
        if self._maintenance_task:
            self._maintenance_task.cancel()
        if self._watch_task:
            self._watch_task.cancel()

        try:
            if self._maintenance_task:
                await self._maintenance_task
            if self._watch_task:
                await self._watch_task
        except asyncio.CancelledError:
            pass

        # Stop all endpoints (active and draining)
        async with self._update_lock:
            all_eps = self.endpoints + self._draining_endpoints
            await asyncio.gather(*[ep.disconnect() for ep in all_eps])
            self.endpoints.clear()
            self._draining_endpoints.clear()
        
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()

    async def _watch_loop(self) -> None:
        """Watch for endpoint updates."""
        try:
            async for configs in self.discovery.watch():
                if not self._started:
                    break
                await self._update_endpoints(configs)
        except Exception as e:
            logger.error(f"Error in discovery watch loop: {e}")

    async def _update_endpoints(self, new_configs: List[EndpointConfig]) -> None:
        """Update internal endpoint list based on discovery."""
        async with self._update_lock:
            current_map = {ep.config.url: ep for ep in self.endpoints}
            new_map = {cfg.url: cfg for cfg in new_configs}
            
            to_remove_urls = set(current_map.keys()) - set(new_map.keys())
            to_add_urls = set(new_map.keys()) - set(current_map.keys())
            
            if not to_remove_urls and not to_add_urls:
                return

            logger.info(f"Updating endpoints: +{len(to_add_urls)} / -{len(to_remove_urls)}")

            # Remove old (Move to draining)
            for url in to_remove_urls:
                ep = current_map[url]
                # Mark as draining and move to draining list
                ep.drain()
                self.endpoints.remove(ep)
                self._draining_endpoints.append(ep)

            # Add new
            for url in to_add_urls:
                cfg = new_map[url]
                if self.playwright:
                    ep = SingleEndpointPool(cfg, self.pool_config, self.playwright)
                    self.endpoints.append(ep)
                    asyncio.create_task(ep.connect())

    async def get_page(self, scene_name: str = "default") -> PageWrapper:
        """Get a page for the specified scene from a healthy endpoint."""
        if not self._started:
            raise PagePoolError("Cluster not started")
            
        scene = self.scenes.get(scene_name)
        if not scene:
            raise PagePoolError(f"Scene '{scene_name}' not defined")

        # Use lock only for copying the list to avoid race conditions with update
        # We don't want to hold lock during get_page logic
        # But endpoints list object is mutable.
        # It's safer to operate on a copy or shallow copy.
        # In Python list operations are atomic-ish but iteration isn't.
        # But since we only append/remove in _update_endpoints with lock...
        # Let's take a snapshot.
        endpoints_snapshot = list(self.endpoints)

        # 1. Filter healthy endpoints
        candidates = [ep for ep in endpoints_snapshot if ep.is_healthy]
        
        if not candidates:
            # Try to recover any circuit-broken endpoint immediately if urgent
            for ep in endpoints_snapshot:
                if ep.check_circuit_recovery():
                    candidates.append(ep)
            
            if not candidates:
                raise PagePoolError("No healthy endpoints available")

        # 2. Select endpoint with retry/failover
        tried_endpoints = set()
        max_retries = min(3, len(candidates))
        
        last_error = None
        
        for _ in range(max_retries):
            # Exclude already tried
            valid_candidates = [ep for ep in candidates if ep not in tried_endpoints]
            if not valid_candidates:
                break
                
            selected_ep = self.strategy.select(valid_candidates)
            if not selected_ep:
                break
                
            tried_endpoints.add(selected_ep)
            
            try:
                # Get specific pool for this scene
                pool = await selected_ep.get_pool(scene)
                return await pool.get_page()
            except Exception as e:
                logger.warning(f"Failed to get page from {selected_ep.config.url}: {e}")
                last_error = e
                # Report failure to endpoint (might trigger circuit breaker)
                selected_ep._handle_connection_error(e)

        raise PagePoolError(f"Failed to get page after retries. Last error: {last_error}")

    async def _maintenance_loop(self) -> None:
        """Background task to monitor endpoints."""
        while self._started:
            try:
                await asyncio.sleep(5.0)
                
                # Snapshot
                endpoints = list(self.endpoints)
                for ep in endpoints:
                    # Check for recovery
                    ep.check_circuit_recovery()
                
                # Cleanup draining endpoints
                for ep in self._draining_endpoints[:]:
                    if ep.is_idle:
                        logger.info(f"Endpoint {ep.config.url} drained completely. Disconnecting.")
                        await ep.disconnect()
                        self._draining_endpoints.remove(ep)
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in maintenance loop: {e}")
                await asyncio.sleep(5.0)