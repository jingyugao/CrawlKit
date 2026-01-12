#!/bin/bash
# Run PagePool tests

set -e

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${YELLOW}PagePool Test Runner${NC}"
echo "================================"

# Check if pytest is installed
if ! python3 -m pytest --version &> /dev/null; then
    echo -e "${RED}pytest not found!${NC}"
    echo "Installing test dependencies..."
    pip install pytest pytest-asyncio playwright
    python3 -m playwright install chromium
fi

# Run tests
echo -e "\n${YELLOW}Running tests...${NC}\n"

# Run with different verbosity levels
if [ "$1" == "-v" ] || [ "$1" == "--verbose" ]; then
    python3 -m pytest tests/test_pagepool.py -v -s --tb=short
elif [ "$1" == "-vv" ]; then
    python3 -m pytest tests/test_pagepool.py -vv -s --tb=long
elif [ "$1" == "-k" ]; then
    # Run specific test
    python3 -m pytest tests/test_pagepool.py -v -s -k "$2"
else
    python3 -m pytest tests/test_pagepool.py --tb=short
fi

# Check exit code
if [ $? -eq 0 ]; then
    echo -e "\n${GREEN}✓ All tests passed!${NC}"
else
    echo -e "\n${RED}✗ Some tests failed${NC}"
    exit 1
fi
