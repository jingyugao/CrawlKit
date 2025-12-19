# pagepool - Playwright Page Pool

High-performance Playwright page pool for web scraping with CDP protocol support.

## Features

- **High Concurrency**: Built on asyncio for handling thousands of concurrent pages
- **Dynamic Endpoint Management**: Support for Chrome cluster auto-scaling (Kubernetes, service discovery)
- **Flexible Load Balancing**: User-defined load balancing strategies
- **Custom Contexts**: User-defined context factory for different scenarios
- **TTL Management**: Control context lifetime (context_ttl)
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
from pagepool import PlaywrightPagePool, PoolConfig

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

## How page acquisition works

The pool manages resources by endpoint:

- **Browser**: `BrowserWrapper` holds the CDP connection, stats, and health.
- **Context**: `ContextWrapper` tracks active pages and TTL per context.
- **Page**: `PageWrapper` carries the page plus its context wrapper and TTL metadata.

When you call `acquire_page()` (or use `async with pool.page()`):

1. **Try idle pages first**  
   The pool pops from the endpoint’s idle `PageWrapper` queue, validates the page/context (not closed, not expired), increments counters, and returns it.

2. **Reuse or create a context**  
   If no idle page is usable, it selects a context that has spare page capacity; otherwise it creates a new context.

3. **Create a new page**  
   On a context with capacity, it creates a fresh page, wraps it, updates stats, and returns it.

On release, the page is either returned to the idle queue for reuse or discarded if TTL/closure conditions apply. Optionally, `min_active_page` triggers background pre-warming of idle pages per endpoint.

### Flowchart (acquire_page)

注：标有 `[I/O]` 的步骤会等待 I/O（CDP 调用或超时）；队列/计数操作为内存级。

```
Start acquire_page
        |
        v
Pop idle PageWrapper? -- yes --> Validate (not closed/expired)?
        |                           |        |
        no                          no       yes
        |                           |        |
        v                           v        v
Select Context with capacity?       Discard  Mark in-use, return page
        |          |
        |          v
        |     Create Context (if under max) [I/O]
        |          |
        |          v
        +------> Context found?
                     |yes
                     v
            Create new page [I/O] -> wrap -> mark in-use -> return
                     |
                     no
                     v
            No capacity -> PageAcquireError
```

## License

MIT
