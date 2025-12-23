#!/bin/bash
set -euo pipefail

CHROME_FLAGS=${CHROME_FLAGS:---remote-debugging-address=127.0.0.1 --remote-debugging-port=9220}
google-chrome $CHROME_FLAGS "$@" &

sleep 2

exec /usr/local/bin/mychrome
