# Quick Start Guide

## 📋 Prerequisites

1. Docker and Docker Compose installed
2. Python 3.10+ installed
3. pip installed

## 🚀 Installation Steps

### 1. Install Python Dependencies

```bash
# Navigate to project root
cd /home/wsl/Code/pwutil

# Install pwutil package and dependencies
pip install -e .

# Or install just playwright
pip install playwright

# Install Playwright browsers
playwright install chromium
```

### 2. Start Docker Services

```bash
# Navigate to examples directory
cd examples

# Start all services (Chrome + Test Web Server)
docker-compose up -d

# Wait a few seconds for services to start
sleep 10

# Verify services are running
docker-compose ps
```

You should see:
- `chrome1` on port 9222
- `chrome2` on port 9223
- `test-web` on port 8080

### 3. Verify Services

```bash
# Test Chrome instance 1
curl http://localhost:9222/json/version

# Test Chrome instance 2
curl http://localhost:9223/json/version

# Test web server
curl http://localhost:8080/health
```

### 4. Run Tests

```bash
# Run comprehensive test suite
python3 test_pool.py

# Or run individual examples
python3 basic_example.py
python3 high_concurrency_example.py
python3 dynamic_endpoints_example.py
python3 custom_context_example.py
```

## 📊 Expected Output

The test script will run 7 comprehensive tests:

1. ✓ Basic Pool Usage
2. ✓ Load Balancing
3. ✓ High Concurrency
4. ✓ TTL Management
5. ✓ Dynamic Endpoints
6. ✓ Custom Context Factory
7. ✓ Error Handling

Each test displays:
- Success/failure status
- Pool statistics
- Performance metrics

## 🛠️ Troubleshooting

### Services not starting

```bash
# Check logs
docker-compose logs chrome1
docker-compose logs chrome2
docker-compose logs test-web

# Restart services
docker-compose restart

# Or rebuild
docker-compose down
docker-compose up -d --build
```

### Python module not found

```bash
# Install in development mode
cd /home/wsl/Code/pwutil
pip install -e .

# Or set PYTHONPATH
export PYTHONPATH=/home/wsl/Code/pwutil:$PYTHONPATH
```

### Playwright browser not found

```bash
# Install Chromium browser
playwright install chromium

# Or install all browsers
playwright install
```

### Port conflicts

Edit `docker-compose.yml` to use different ports:

```yaml
ports:
  - "19222:3000"  # Change from 9222
```

## 🧹 Cleanup

```bash
# Stop all services
docker-compose down

# Remove volumes
docker-compose down -v

# Remove images
docker-compose down --rmi all
```

## 📝 Next Steps

After successful testing:

1. Read the main README: `../README.md`
2. Check the implementation plan: `../.claude/plans/dynamic-wondering-spring.md`
3. Explore source code: `../pwutil/`
4. Customize for your use case

## 🎯 Quick Test (Minimal)

If you just want to verify basic functionality:

```python
import asyncio
from pwutil import PlaywrightPagePool, PoolConfig

async def main():
    config = PoolConfig(cdp_endpoints=['http://localhost:9222'])

    async with PlaywrightPagePool(config) as pool:
        async with pool.page() as page:
            await page.goto('http://localhost:8080/')
            print(f"Title: {await page.title()}")

asyncio.run(main())
```

Save as `quick_test.py` and run:
```bash
python3 quick_test.py
```
