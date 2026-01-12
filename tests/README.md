# PagePool Tests

Comprehensive test suite for the PagePool implementation using pytest.

## Test Coverage

The test suite covers all major functionality:

### 1. Basic Functionality (`TestBasicFunctionality`)
- ✅ Pool start creates idle pages (warmup)
- ✅ Get and release page flow
- ✅ Context manager usage
- ✅ Page cleared to `about:blank` on release

### 2. Health Check (`TestHealthCheck`)
- ✅ Default health check (browser.is_connected)
- ✅ Custom health check function
- ✅ Unhealthy page retry logic (max 3 retries)

### 3. Idle Page Management (`TestIdlePageManagement`)
- ✅ Background refill triggered when idle < min
- ✅ Refill respects max_total_pages limit

### 4. Total Pages Limit (`TestTotalPagesLimit`)
- ✅ Pages destroyed when total >= max_total_pages
- ✅ max_idle_pages limit enforcement

### 5. Page Usage Limit (`TestPageUsageLimit`)
- ✅ Pages destroyed after max_page_uses

### 6. Context TTL (`TestContextTTL`)
- ✅ Context rotation when approaching TTL
- ✅ Draining contexts destroyed after pages released

### 7. Max Pages Per Context (`TestMaxPagesPerContext`)
- ✅ New context created when limit reached

### 8. Lifecycle (`TestLifecycle`)
- ✅ Pool not started error
- ✅ Stop closes all contexts
- ✅ Context manager lifecycle

### 9. Concurrency (`TestConcurrency`)
- ✅ Concurrent page usage

### 10. Statistics (`TestStatistics`)
- ✅ get_stats() returns correct info
- ✅ Stats update on get/release

## Installation

Install test dependencies:

```bash
# Install pytest and dependencies
pip install pytest pytest-asyncio playwright

# Install Playwright browsers
python3 -m playwright install chromium
```

## Running Tests

### Quick Run (all tests)

```bash
# From project root
python3 -m pytest tests/test_pagepool.py

# Or use the run script
chmod +x tests/run_tests.sh
./tests/run_tests.sh
```

### Verbose Output

```bash
# Show test names
./tests/run_tests.sh -v

# Very verbose (show print statements)
./tests/run_tests.sh -vv
```

### Run Specific Tests

```bash
# Run tests matching a pattern
./tests/run_tests.sh -k "health_check"

# Run a specific test class
python3 -m pytest tests/test_pagepool.py::TestBasicFunctionality -v

# Run a specific test method
python3 -m pytest tests/test_pagepool.py::TestBasicFunctionality::test_get_and_release_page -v
```

### Show Coverage

```bash
# Install coverage tool
pip install pytest-cov

# Run with coverage
python3 -m pytest tests/test_pagepool.py --cov=pagepool --cov-report=html

# View coverage report
open htmlcov/index.html
```

## Test Structure

Each test class focuses on a specific feature:

```
test_pagepool.py
├── TestBasicFunctionality     - Core operations
├── TestHealthCheck            - Health checking
├── TestIdlePageManagement     - Idle queue and refill
├── TestTotalPagesLimit        - Total/idle limits
├── TestPageUsageLimit         - Usage count tracking
├── TestContextTTL             - TTL rotation
├── TestMaxPagesPerContext     - Per-context limits
├── TestLifecycle              - Start/stop
├── TestConcurrency            - Concurrent usage
└── TestStatistics             - Stats API
```

## Fixtures

- `browser`: Provides a Playwright browser instance
- `simple_pool`: Provides a started PagePool with default config

## Common Issues

### Browser Not Found

If you get "Executable doesn't exist" error:

```bash
python3 -m playwright install chromium
```

### Asyncio Warnings

If you see event loop warnings, ensure `pytest-asyncio` is installed:

```bash
pip install pytest-asyncio
```

### Test Timeouts

Some tests use `asyncio.sleep()` to wait for async operations. If tests are flaky:

1. Increase sleep durations in the test
2. Or run with `--timeout=30` flag:

```bash
pip install pytest-timeout
python3 -m pytest tests/test_pagepool.py --timeout=30
```

## Writing New Tests

To add a new test:

1. Create a test class or add to existing class
2. Use `@pytest.mark.asyncio` for async tests
3. Use fixtures for browser/pool setup
4. Follow naming convention: `test_<feature_description>`

Example:

```python
class TestMyFeature:
    @pytest.mark.asyncio
    async def test_my_feature(self, browser: Browser):
        """Test description."""
        # Test code here
        pass
```

## CI/CD Integration

For CI/CD pipelines (GitHub Actions, GitLab CI, etc.):

```yaml
# Example GitHub Actions
- name: Install dependencies
  run: |
    pip install pytest pytest-asyncio playwright
    python3 -m playwright install chromium

- name: Run tests
  run: python3 -m pytest tests/test_pagepool.py -v
```

## Performance Notes

- Tests use `chromium.launch()` which is slower than CDP connection
- For faster tests, consider using a persistent browser with CDP
- Total test runtime: ~30-60 seconds (depending on machine)
