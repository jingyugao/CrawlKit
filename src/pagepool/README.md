# PagePool - Simplified Playwright Page Pool

A simplified page pool for managing Playwright pages in a single browser with advanced features like context TTL rotation, page usage limits, and health checking.

## Features

- **Idle Page Queue**: Maintains a queue of pre-created pages for instant allocation
- **Health Checking**: Validates pages before reuse with configurable health checks (max 3 retries)
- **Context TTL Rotation**: Automatically rotates browser contexts based on TTL
- **Page Usage Limits**: Destroys pages after a configurable number of uses
- **Total Page Limits**: Enforces maximum total pages (idle + active)
- **Auto-refill**: Background task to maintain minimum idle pages
- **Page Cleanup**: Navigates to `about:blank` before returning pages to pool

## Installation

The module is part of the `pwutil` package. Requires:
- `playwright >= 1.40.0`
- Python 3.10+

## Quick Start

```python
from playwright.async_api import async_playwright
from pagepool import PagePool

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()

        # Create pool
        async def create_context():
            return await browser.new_context(
                viewport={'width': 1920, 'height': 1080}
            )

        pool = PagePool(
            browser=browser,
            new_context_func=create_context,
            min_idle_pages=10,
            max_total_pages=100,
        )

        await pool.start()

        try:
            # Use context manager (recommended)
            async with pool.page() as page:
                await page.goto('https://example.com')
                print(await page.title())
        finally:
            await pool.stop()
```

## Configuration Parameters

### Constructor

```python
PagePool(
    browser: Browser,                    # Connected Playwright browser
    new_context_func: Callable,          # Function to create contexts
    min_idle_pages: int = 5,             # Minimum idle pages to maintain
    max_total_pages: int = 100,          # Maximum total pages (idle + active)
    max_idle_pages: int = 0,             # Maximum idle pages (0 = auto)
    max_pages_per_context: int = 0,      # Pages per context limit (0 = unlimited)
    context_ttl: float | None = None,    # Context lifetime in seconds
    max_page_uses: int | None = None,    # Max uses per page
    health_check: Callable | None = None # Custom health check function
)
```

### Parameter Details

- **`browser`**: An already-connected Playwright Browser object
- **`new_context_func`**: Sync or async function that returns a BrowserContext
- **`min_idle_pages`**: Pool always maintains at least this many idle pages (default: 5)
- **`max_total_pages`**: Hard limit for total pages, includes both idle and active (default: 100)
- **`max_idle_pages`**: Soft limit for idle pages only (default: 0 = unlimited, bounded by total)
- **`max_pages_per_context`**: Maximum pages per context (default: 0 = unlimited)
- **`context_ttl`**: Context lifetime in seconds, rotates before expiry (default: None = no TTL)
- **`max_page_uses`**: Page is destroyed after this many uses (default: None = unlimited)
- **`health_check`**: Custom health check function (default: checks browser.is_connected)

## Usage Patterns

### Context Manager (Recommended)

```python
async with pool.page() as page:
    await page.goto('https://example.com')
    # Page automatically released after use
```

### Manual Acquire/Release

```python
page_wrapper = await pool.get_page()
try:
    await page_wrapper.obj.goto('https://example.com')
finally:
    await pool.release_page(page_wrapper)
```

### Concurrent Usage

```python
async def fetch(url):
    async with pool.page() as page:
        await page.goto(url)
        return await page.title()

# Fetch multiple pages concurrently
results = await asyncio.gather(*[fetch(url) for url in urls])
```

### Custom Health Check

```python
async def my_health_check(page_wrapper):
    """Verify page can execute JavaScript."""
    try:
        result = await asyncio.wait_for(
            page_wrapper.obj.evaluate("1 + 1"),
            timeout=1.0
        )
        return result == 2
    except:
        return False

pool = PagePool(
    browser=browser,
    new_context_func=create_context,
    health_check=my_health_check
)
```

## How It Works

### Page Acquisition Flow

1. Try to get page from idle queue (max 3 retries):
   - Get page from queue
   - Run health check (default: `browser.is_connected()`)
   - If healthy: increment use count, return page
   - If unhealthy: discard and retry
2. If all retries fail or queue empty: create new page
3. After getting page:
   - Trigger background refill if idle < min_idle_pages
   - Check and rotate expiring contexts

### Page Release Flow

1. Decrement context active page count
2. Check various conditions (in order):
   - Context draining → discard page
   - Page closed or max uses exceeded → discard page
   - Context TTL expired → discard page, destroy context if last
   - Total pages >= max_total_pages → discard page
   - Idle pages >= max_idle_pages → discard page
3. Otherwise: navigate to `about:blank` and return to pool
4. Check and rotate expiring contexts

### Context TTL Rotation

- Contexts approaching TTL (within 10% or 5s) are marked as "draining"
- New context is created as replacement
- Idle pages from draining context are purged
- Draining context is destroyed when last active page is released

### Health Check

- Default: checks `browser.is_connected()` and page not closed
- Custom: provide your own sync or async function
- Called on each page from idle queue
- Max 3 retries before creating new page

## Statistics

Get pool statistics at any time:

```python
stats = pool.get_stats()
# Returns:
# {
#     'total_contexts': 2,
#     'draining_contexts': 0,
#     'idle_pages': 8,
#     'active_pages': 5,
#     'total_pages': 13,
#     'started': True
# }
```

## Best Practices

1. **Always use context manager**: Ensures pages are properly released
2. **Set reasonable limits**: Tune `min_idle_pages` and `max_total_pages` for your workload
3. **Use context TTL**: Prevents memory leaks from long-running contexts
4. **Monitor stats**: Use `get_stats()` to understand pool behavior
5. **Custom health checks**: Add domain-specific validation if needed

## Example Configurations

### High-concurrency scraping (many short tasks)

```python
pool = PagePool(
    browser=browser,
    new_context_func=create_context,
    min_idle_pages=20,        # Keep many pages ready
    max_total_pages=100,      # Allow high concurrency
    max_idle_pages=30,        # Limit idle overhead
    max_page_uses=100,        # Rotate pages frequently
    context_ttl=600.0,        # Rotate contexts every 10 min
)
```

### Low-concurrency automation (few long tasks)

```python
pool = PagePool(
    browser=browser,
    new_context_func=create_context,
    min_idle_pages=2,         # Minimal idle pages
    max_total_pages=10,       # Low concurrency
    max_page_uses=50,         # Less frequent rotation
    context_ttl=1800.0,       # Rotate contexts every 30 min
)
```

### Resource-constrained environment

```python
pool = PagePool(
    browser=browser,
    new_context_func=create_context,
    min_idle_pages=1,         # Minimal resources
    max_total_pages=5,        # Very low limit
    max_pages_per_context=2,  # Small contexts
    context_ttl=300.0,        # Frequent rotation
)
```

## See Also

- Example: `/example/pagepool_example.py`
- Plan: `/home/wsl/.claude/plans/floofy-beaming-puzzle.md`
