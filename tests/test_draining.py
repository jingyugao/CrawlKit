import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import Browser, BrowserContext, Page

from pagepool.cluster import ClusterPagePool
from pagepool.discovery import EndpointDiscovery
from pagepool.types import EndpointConfig, PoolConfig, SceneConfig
from pagepool.wrappers import PageWrapper

# 1. Mock Discovery: 模拟 K8S Watch 机制
class MockK8SDiscovery(EndpointDiscovery):
    def __init__(self):
        self.queue = asyncio.Queue()

    async def watch(self):
        """Simulate k8s watch stream"""
        while True:
            yield await self.queue.get()
            
    async def scale_update(self, endpoints):
        """Helper to trigger scaling events"""
        await self.queue.put(endpoints)

# 2. Mock Playwright
@pytest.fixture
def mock_playwright_cluster():
    with patch("pagepool.cluster.async_playwright") as mock:
        pw_instance = AsyncMock()
        cm_mock = MagicMock()
        cm_mock.start = AsyncMock(return_value=pw_instance)
        mock.return_value = cm_mock
        
        # Factory to create distinct browser mocks for each endpoint
        def create_browser_mock(url):
            browser = AsyncMock(spec=Browser)
            browser.url = url  # Tag for verification
            browser.is_connected.return_value = True
            browser.contexts = []
            
            # Mock new_context / new_page
            context = AsyncMock(spec=BrowserContext)
            page = AsyncMock(spec=Page)
            page.is_closed.return_value = False
            
            context.new_page.return_value = page
            browser.new_context.return_value = context
            
            # Mock 'on' event listener
            browser.on = MagicMock()
            
            return browser

        # Side effect to return different browsers based on connect URL
        browsers = {}
        async def connect_side_effect(url, **kwargs):
            if url not in browsers:
                browsers[url] = create_browser_mock(url)
            return browsers[url]
            
        pw_instance.chromium.connect.side_effect = connect_side_effect
        
        yield pw_instance, browsers

@pytest.mark.asyncio
async def test_k8s_scaling_scenario(mock_playwright_cluster):
    _, browsers_map = mock_playwright_cluster
    
    # Define Pods
    pod_a = EndpointConfig(url="ws://pod-a")
    pod_b = EndpointConfig(url="ws://pod-b")
    
    discovery = MockK8SDiscovery()
    
    # ---------------------------------------------------------
    # Phase 1: Initial Scale (2 Pods)
    # ---------------------------------------------------------
    cluster = ClusterPagePool(discovery, PoolConfig(min_idle=0))
    await cluster.start()
    
    # K8S reports 2 pods
    await discovery.scale_update([pod_a, pod_b])
    await asyncio.sleep(0.1) # Wait for connect
    
    assert len(cluster.endpoints) == 2
    
    # ---------------------------------------------------------
    # Phase 2: Start "Long Running Task" on Pod A
    # ---------------------------------------------------------
    
    # Directly pick Pod A from the cluster to ensure we have a task running on it
    # This simulates a request having been routed there by the LoadBalancer
    ep_a = next(ep for ep in cluster.endpoints if ep.config.url == "ws://pod-a")
    pool_a = await ep_a.get_pool(SceneConfig("default"))
    
    # Simulate getting a page (routed to this endpoint)
    task_page_wrapper = await pool_a.get_page()
    
    print(f"\n[Test] Started Task on {ep_a.config.url}")
    assert ep_a.load == 1
    
    # ---------------------------------------------------------
    # Phase 3: Scale Down (Remove Pod A)
    # ---------------------------------------------------------
    print("[Test] K8S Scale Down: Removing Pod A")
    await discovery.scale_update([pod_b]) # Only Pod B remains
    await asyncio.sleep(0.1)
    
    # ---------------------------------------------------------
    # Phase 4: Verify Isolation & Survival
    # ---------------------------------------------------------
    # 1. Pod A should be gone from active endpoints
    assert len(cluster.endpoints) == 1
    assert cluster.endpoints[0].config.url == "ws://pod-b"
    
    # 2. Pod A should be in draining list
    assert len(cluster._draining_endpoints) == 1
    draining_ep = cluster._draining_endpoints[0]
    assert draining_ep.config.url == "ws://pod-a"
    assert draining_ep._draining is True
    
    # 3. Connection to Pod A should still be OPEN (Browser not closed)
    # Because we have 1 active page
    browser_a = browsers_map["ws://pod-a"]
    browser_a.close.assert_not_called()
    print("[Test] Verified: Pod A connection is still alive for old task")
    
    # 4. Old page should still be usable
    # (Mock doesn't actually run JS, but object is valid)
    assert not task_page_wrapper.obj.is_closed()
    
    # 5. New requests should NOT go to Pod A
    print("[Test] Making new request...")
    new_page = await cluster.get_page()
    
    # Verify new page is on Pod B
    # Check if Pod B's browser created a new context
    browser_b = browsers_map["ws://pod-b"]
    assert browser_b.new_context.call_count >= 1
    
    # Double check draining ep load didn't increase
    assert draining_ep.load == 1 # Still just the old task
    
    # ---------------------------------------------------------
    # Phase 5: Cleanup & disconnect
    # ---------------------------------------------------------
    print("[Test] Task on Pod A finished. Releasing...")
    await pool_a.release_page(task_page_wrapper)
    
    assert draining_ep.load == 0
    
    # Trigger maintenance loop logic manually for speed
    if draining_ep.is_idle:
        await draining_ep.disconnect()
        cluster._draining_endpoints.remove(draining_ep)
    
    # Verify Pod A is now truly disconnected
    browser_a.close.assert_called_once()
    print("[Test] Verified: Pod A disconnected after drain")
    
    await cluster.stop()