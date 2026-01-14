import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import Browser, BrowserContext, Page

from pagepool.cluster import ClusterPagePool
from pagepool.types import EndpointConfig, PoolConfig, SceneConfig

@pytest.fixture
def mock_playwright():
    with patch("pagepool.cluster.async_playwright") as mock:
        pw_instance = AsyncMock()
        
        # async_playwright() returns a ContextManager
        # context_manager.start() is an async method that returns the Playwright instance
        cm_mock = MagicMock()
        cm_mock.start = AsyncMock(return_value=pw_instance)
        
        mock.return_value = cm_mock
        
        # Mock chromium.connect
        browser_mock = AsyncMock(spec=Browser)
        browser_mock.new_context.return_value = AsyncMock(spec=BrowserContext)
        browser_mock.new_context.return_value.new_page.return_value = AsyncMock(spec=Page)
        browser_mock.is_connected.return_value = True
        
        pw_instance.chromium.connect.return_value = browser_mock
        
        yield pw_instance

@pytest.mark.asyncio
async def test_cluster_start_stop(mock_playwright):
    endpoints = [
        EndpointConfig(url="ws://endpoint1"),
        EndpointConfig(url="ws://endpoint2")
    ]
    
    cluster = ClusterPagePool(endpoints)
    await cluster.start()
    
    # Wait for background connect tasks
    await asyncio.sleep(0.1)
    
    assert cluster._started
    assert len(cluster.endpoints) == 2
    
    # Verify connect called for each
    assert mock_playwright.chromium.connect.call_count == 2
    
    await cluster.stop()
    assert not cluster._started

@pytest.mark.asyncio
async def test_load_balancing_round_robin(mock_playwright):
    # Setup 2 distinct mock browsers to verify distribution
    browser1 = AsyncMock(spec=Browser)
    browser1.is_connected.return_value = True
    browser1.new_context.return_value.new_page.return_value = AsyncMock(spec=Page)
    
    browser2 = AsyncMock(spec=Browser)
    browser2.is_connected.return_value = True
    browser2.new_context.return_value.new_page.return_value = AsyncMock(spec=Page)
    
    # Ensure side_effect returns awaitables
    async def connect_side_effect(*args, **kwargs):
        if connect_side_effect.count == 0:
            connect_side_effect.count += 1
            return browser1
        return browser2
    connect_side_effect.count = 0
    
    mock_playwright.chromium.connect.side_effect = connect_side_effect
    
    endpoints = [
        EndpointConfig(url="ws://e1"),
        EndpointConfig(url="ws://e2")
    ]
    
    async with ClusterPagePool(endpoints) as cluster:
        # Give time for async connect
        await asyncio.sleep(0.1)
        
        # Request 1 -> should go to endpoint 1 (index 0)
        await cluster.get_page()
        
        # Request 2 -> should go to endpoint 2 (index 1)
        await cluster.get_page()
        
        # Verify both browsers were used
        # Note: Depending on which task finished first, the order of connect calls might vary,
        # but both should be used by RR strategy if they are healthy.
        # Since we use RR, and tasks start in order, we expect distribution.
        
        # We check that we got contexts from both browsers
        assert len(browser1.new_context.mock_calls) >= 1
        assert len(browser2.new_context.mock_calls) >= 1

class DummyBrowser:
    def __init__(self, fail=False):
        self.fail = fail
        self.connected = True
        self.new_context_called = False

    async def new_context(self, **kwargs):
        self.new_context_called = True
        if self.fail:
            raise Exception("Connection lost")
        
        # Return a mock context that behaves nicely
        page_mock = AsyncMock(spec=Page)
        page_mock.is_closed.return_value = False
        context_mock = AsyncMock(spec=BrowserContext)
        context_mock.new_page.return_value = page_mock
        return context_mock

    def is_connected(self):
        return self.connected

    async def close(self):
        self.connected = False

    def on(self, event, cb):
        pass

@pytest.mark.asyncio
async def test_failover(mock_playwright):
    # Browser 1 fails
    browser1 = DummyBrowser(fail=True)
    # Browser 2 works
    browser2 = DummyBrowser(fail=False)
    
    async def connect_side_effect(*args, **kwargs):
        url = args[0]
        if url == "ws://bad":
            return browser1
        return browser2
    
    mock_playwright.chromium.connect.side_effect = connect_side_effect
    
    endpoints = [
        EndpointConfig(url="ws://bad"),
        EndpointConfig(url="ws://good")
    ]
    
    async with ClusterPagePool(endpoints) as cluster:
        await asyncio.sleep(0.2)
        
        # Should succeed eventually by hitting browser2
        page_wrapper = await cluster.get_page()
        assert page_wrapper is not None
        
        # Verify browser1 was tried and failed
        assert browser1.new_context_called
        # Verify browser2 was used
        assert browser2.new_context_called

@pytest.mark.asyncio
async def test_scene_configuration(mock_playwright):
    browser = AsyncMock(spec=Browser)
    browser.is_connected.return_value = True
    browser.new_context.return_value.new_page.return_value = AsyncMock(spec=Page)
    mock_playwright.chromium.connect.return_value = browser
    
    cluster = ClusterPagePool([EndpointConfig(url="ws://e1")])
    
    # Define mobile scene
    mobile_scene = SceneConfig(
        name="mobile",
        user_agent="MobileSafari/1.0",
        viewport={"width": 375, "height": 667}
    )
    cluster.define_scene(mobile_scene)
    
    async with cluster:
        await asyncio.sleep(0.1)
        
        await cluster.get_page("mobile")
        
        # Verify new_context was called with correct args
        call_kwargs = browser.new_context.call_args.kwargs
        assert call_kwargs["user_agent"] == "MobileSafari/1.0"
        assert call_kwargs["viewport"] == {"width": 375, "height": 667}
