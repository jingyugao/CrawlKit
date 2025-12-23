# pwutil - Playwright Page Pool

High-performance Playwright page pool for web scraping with CDP protocol support.

## Features

- **High Concurrency**: Built on asyncio for handling thousands of concurrent pages
- **Dynamic Endpoint Management**: Support for Chrome cluster auto-scaling (Kubernetes, service discovery)
- **Flexible Load Balancing**: User-defined load balancing strategies
- **Custom Contexts**: User-defined context factory for different scenarios
- **TTL Management**: Control page and context lifetime (page_ttl, context_ttl)
- **Robust Error Handling**: Circuit breaker, retry logic, graceful degradation
- **Resource Efficient**: Connection/context reuse, idle cleanup, TTL auto-cleanup
- **Easy to Use**: Context manager support, simple API
- **Observable**: Rich statistics for monitoring
- **Cloud Native**: Kubernetes, service discovery, config center integration

## Installation

Prefer using [uv](https://github.com/astral-sh/uv) for deterministic, Python 3.10+ compatible environments:

```bash
uv venv
source .venv/bin/activate
uv pip sync uv.lock
uv run playwright install chromium
```

If you cannot use uv, you can fall back to pip:

```bash
pip install -r requirements.txt
playwright install chromium
```

## Quick Start

```python
import asyncio
from pwutil import PlaywrightPagePool, PoolConfig

async def main():
    config = PoolConfig(
        cdp_endpoints=['http://localhost:9222']
    )

    async with PlaywrightPagePool(config) as pool:
        async with pool.page() as page:
            await page.goto('https://example.com')
            title = await page.title()
            print(f"Title: {title}")

if __name__ == '__main__':
    asyncio.run(main())
```

## Documentation

See the `examples/` directory for more usage patterns.

## License

MIT
