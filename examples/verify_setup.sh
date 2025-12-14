#!/bin/bash

echo "🔍 Verifying pagepool Docker Environment"
echo "======================================"
echo ""

# Check if docker-compose is running
echo "📦 Checking Docker services..."
docker-compose ps

echo ""
echo "🌐 Testing Chrome Instance 1 (port 9222)..."
if curl -s http://localhost:9222/json/version > /dev/null 2>&1; then
    echo "✓ Chrome 1 is responding"
    curl -s http://localhost:9222/json/version | python3 -c "import sys, json; data=json.load(sys.stdin); print(f'  Browser: {data[\"Browser\"]}')"
else
    echo "❌ Chrome 1 is not responding"
fi

echo ""
echo "🌐 Testing Chrome Instance 2 (port 9223)..."
if curl -s http://localhost:9223/json/version > /dev/null 2>&1; then
    echo "✓ Chrome 2 is responding"
    curl -s http://localhost:9223/json/version | python3 -c "import sys, json; data=json.load(sys.stdin); print(f'  Browser: {data[\"Browser\"]}')"
else
    echo "❌ Chrome 2 is not responding"
fi

echo ""
echo "🌐 Testing Test Web Server (port 8080)..."
if curl -s http://localhost:8080/health > /dev/null 2>&1; then
    echo "✓ Test web server is responding"
    curl -s http://localhost:8080/health | python3 -c "import sys, json; data=json.load(sys.stdin); print(f'  Status: {data[\"status\"]}, Requests: {data[\"requests\"]}')"
else
    echo "❌ Test web server is not responding"
fi

echo ""
echo "======================================"
echo "✅ Docker environment verification complete!"
echo ""
echo "Next steps:"
echo "1. Install Python dependencies:"
echo "   pip install playwright"
echo "   playwright install chromium"
echo ""
echo "2. Run tests:"
echo "   python3 test_pool.py"
echo ""
echo "3. Or run individual examples:"
echo "   python3 basic_example.py"
