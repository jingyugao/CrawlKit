#!/bin/bash
set -e
google-chrome $@  &

sleep 2

socat TCP-LISTEN:9222,fork,reuseaddr TCP:127.0.0.1:9220 
