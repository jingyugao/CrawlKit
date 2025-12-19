# pagepool Examples and Testing

This directory contains examples and a complete testing environment for pagepool.

## 🐳 Docker Environment

The Docker environment includes:
- **chrome1**: Chrome instance on port 9222
- **chrome2**: Chrome instance on port 9223
- **test-web**: Test web server on port 8080

## 🚀 Quick Start

### 1. Start Docker Services

```bash
cd examples
docker-compose up -d
```

Wait a few seconds for services to start, then verify:

```bash
docker-compose ps
```

You should see all three services running.

### 2. Verify Services

Check Chrome instances:
```bash
curl http://localhost:9222/json/version
curl http://localhost:9223/json/version
```

Check test web server:
```bash
curl http://localhost:8080/
```

### 3. Run Tests

```bash
# Make sure you're in the examples directory
cd examples

# Run comprehensive tests
python test_pool.py
```

## 📊 Test Coverage

The test script (`test_pool.py`) covers:

1. **Basic Usage** - Simple page acquisition and scraping
2. **Load Balancing** - Distribution across multiple Chrome instances
3. **High Concurrency** - 50+ concurrent page requests
4. **TTL Management** - Page and context TTL verification
5. **Dynamic Endpoints** - Adding/removing endpoints at runtime
6. **Custom Context** - Mobile viewport and custom settings
7. **Error Handling** - Circuit breaker and failover

## 📝 Example Scripts

### Basic Example
```bash
python basic_example.py
```
Simple usage with context managers.

### High Concurrency Example
```bash
python high_concurrency_example.py
```
Scraping 100 URLs with batching and statistics.

### Dynamic Endpoints Example
```bash
python dynamic_endpoints_example.py
```
Demonstrates endpoint auto-discovery and scaling.

### Custom Context Example
```bash
python custom_context_example.py
```
Mobile/desktop contexts and custom load balancing.

## 🌐 Test Web Server

The test web server provides several endpoints:

- `http://localhost:8080/` - Home page
- `http://localhost:8080/page/1` - Dynamic pages (1, 2, 3, ...)
- `http://localhost:8080/slow` - Slow loading page (2-4s)
- `http://localhost:8080/api/stats` - Statistics JSON
- `http://localhost:8080/health` - Health check

Each page displays:
- Request timestamp
- User agent
- Total request count
- Response time
- Random content (changes each request)

## 🛠️ Troubleshooting

### Chrome instances not starting

Check logs:
```bash
docker-compose logs chrome1
docker-compose logs chrome2
```

Increase shared memory:
```bash
# Already configured in docker-compose.yml
shm_size: 2gb
```

### Test web server issues

Check logs:
```bash
docker-compose logs test-web
```

Rebuild:
```bash
docker-compose build test-web
docker-compose up -d test-web
```

### Port conflicts

If ports are already in use, modify `docker-compose.yml`:
```yaml
ports:
  - "19222:3000"  # Change 9222 to 19222
```

### Connection refused errors

Wait a bit longer for services to fully start:
```bash
docker-compose ps
# All services should show "Up"
```

## 🧹 Cleanup

Stop and remove all containers:
```bash
docker-compose down
```

Remove volumes (if needed):
```bash
docker-compose down -v
```

## 📈 Performance Tips

1. **Adjust pool size** based on Chrome instance capacity:
   ```python
   config = PoolConfig(
   )
   ```

2. **Enable context reuse** for better performance:
   ```python
   config = PoolConfig(reuse_contexts=True)
   ```

3. **Use batching** for many URLs:
   ```python
   batch_size = 20
   for i in range(0, len(urls), batch_size):
       batch = urls[i:i + batch_size]
       results = await asyncio.gather(*[scrape(url) for url in batch])
   ```

4. **Monitor statistics** to optimize:
   ```python
   stats = pool.get_stats()
   for endpoint, stat in stats.items():
       print(f"{endpoint}: {stat.load_score}")
   ```

## 📚 Additional Resources

- Main README: `../README.md`
- Plan file: `../.claude/plans/dynamic-wondering-spring.md`
- Source code: `../pagepool/`
