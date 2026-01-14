import asyncio
import logging
from typing import Dict, Optional

from playwright.async_api import Browser, Playwright, async_playwright

from pagepool.pool import PagePool
from pagepool.types import EndpointConfig, PoolConfig, SceneConfig

logger = logging.getLogger(__name__)

class SingleEndpointPool:
    """Manages a connection to a single CDP endpoint and its associated PagePools per scene."""

    def __init__(
        self,
        config: EndpointConfig,
        pool_config: PoolConfig,
        playwright: Playwright,
    ):
        self.config = config
        self.pool_config = pool_config
        self.playwright = playwright
        
        self.browser: Optional[Browser] = None
        self.pools: Dict[str, PagePool] = {}  # scene_name -> PagePool
        self._connected = False
        self._circuit_open = False
        self._draining = False
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._lock = asyncio.Lock()

    @property
    def is_healthy(self) -> bool:
        return self._connected and not self._circuit_open and not self._draining

    @property
    def load(self) -> int:
        """Current total active pages across all scenes."""
        return sum(pool.get_stats()["active_pages"] for pool in self.pools.values())

    @property
    def is_idle(self) -> bool:
        """True if no pages are currently active."""
        return self.load == 0

    def drain(self) -> None:
        """Mark as draining. No new pools/pages, but keep existing."""
        if not self._draining:
            self._draining = True
            logger.info(f"Endpoint {self.config.url} marked for draining")

    async def connect(self) -> None:
        """Connect to the CDP endpoint."""
        logger.debug(f"Connect called for {self.config.url}")
        async with self._lock:
            logger.debug(f"Connect lock acquired for {self.config.url}")
            if self._connected:
                logger.debug(f"Already connected to {self.config.url}")
                return

            try:
                logger.info(f"Connecting to CDP endpoint: {self.config.url}")
                self.browser = await self.playwright.chromium.connect(
                    self.config.url,
                    timeout=10000
                )
                logger.debug(f"Playwright connect returned for {self.config.url}")
                self.browser.on("disconnected", self._on_disconnected)
                self._connected = True
                self._failure_count = 0
                logger.info(f"Connected to {self.config.url}")
            except Exception as e:
                logger.debug(f"Connect failed for {self.config.url}: {e}")
                self._handle_connection_error(e)
                raise
            finally:
                logger.debug(f"Connect lock releasing for {self.config.url}")

    async def disconnect(self) -> None:
        """Disconnect and stop all pools."""
        async with self._lock:
            if not self._connected:
                return

            logger.info(f"Disconnecting from {self.config.url}")
            
            # Stop all pools first
            for pool in self.pools.values():
                await pool.stop()
            self.pools.clear()

            if self.browser:
                await self.browser.close()
                self.browser = None
            
            self._connected = False

    def _on_disconnected(self, browser: Browser):
        """Handle browser disconnection."""
        logger.warning(f"Browser disconnected: {self.config.url}")
        self._connected = False
        # Logic to trigger reconnection could go here or be handled by the cluster manager

    def _handle_connection_error(self, error: Exception):
        """Update circuit breaker state on error."""
        self._failure_count += 1
        logger.error(f"Connection error for {self.config.url}: {error} (Failures: {self._failure_count})")
        
        if self._failure_count >= self.config.failure_threshold:
            self._circuit_open = True
            import time
            self._last_failure_time = time.time()
            logger.warning(f"Circuit breaker OPEN for {self.config.url}")

    async def get_pool(self, scene: SceneConfig) -> PagePool:
        """Get or create a PagePool for the specific scene."""
        logger.debug(f"get_pool called for {self.config.url}")
        if not self._connected:
            # Try to reconnect if not connected (lazy connect)
            logger.debug(f"Not connected, calling connect for {self.config.url}")
            await self.connect()

        if scene.name not in self.pools:
            logger.debug(f"Scene not in pools, waiting for lock {self.config.url}")
            async with self._lock:
                logger.debug(f"Lock acquired in get_pool {self.config.url}")
                if scene.name not in self.pools:
                    pool = await self._create_pool_for_scene(scene)
                    await pool.start()
                    self.pools[scene.name] = pool
        
        return self.pools[scene.name]

    async def _create_pool_for_scene(self, scene: SceneConfig) -> PagePool:
        """Create a new PagePool configured for the scene."""
        if not self.browser:
            raise RuntimeError("Browser not connected")

        async def create_context():
            # Apply scene configuration
            options = scene.context_options.copy()
            if scene.user_agent:
                options["user_agent"] = scene.user_agent
            if scene.viewport:
                options["viewport"] = scene.viewport
            if scene.proxy:
                options["proxy"] = scene.proxy
            if scene.geolocation:
                options["geolocation"] = scene.geolocation
            if scene.permissions:
                options["permissions"] = scene.permissions
            if scene.extra_http_headers:
                options["extra_http_headers"] = scene.extra_http_headers
            
            return await self.browser.new_context(**options)

        return PagePool(
            browser=self.browser,
            new_context_func=create_context,
            min_idle_pages=self.pool_config.min_idle,
            max_total_pages=self.pool_config.max_total,
            context_ttl=self.pool_config.context_ttl,
            max_page_uses=self.pool_config.max_context_uses,
        )

    def check_circuit_recovery(self) -> bool:
        """Check if circuit breaker should attempt recovery."""
        if not self._circuit_open:
            return True
        
        import time
        if time.time() - self._last_failure_time > self.config.recovery_timeout:
            logger.info(f"Circuit breaker recovery timeout reached for {self.config.url}, attempting recovery")
            self._circuit_open = False
            self._failure_count = 0
            return True
        return False
