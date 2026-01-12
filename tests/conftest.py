"""Pytest configuration for pagepool tests."""
import pytest


def pytest_configure(config):
    """Configure pytest with custom markers."""
    config.addinivalue_line(
        "markers", "asyncio: mark test as async"
    )


@pytest.fixture(scope="session")
def event_loop_policy():
    """Use default event loop policy."""
    import asyncio
    return asyncio.DefaultEventLoopPolicy()
