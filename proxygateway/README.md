# proxygateway

A lightweight HTTPS tunnel proxy gateway that selects upstream proxies based on user policy.

## Features
- HTTPS CONNECT proxy server
- Per-user policy filters (TTL, remaining QPS, min max-QPS)
- Optional channel binding (`user:password:channel`)
- Auto-fetch proxies from an allocator API when pool is empty

## Config
Create `config.json`:

```json
{
  "listen_addr": ":8443",
  "tls_cert_file": "",
  "tls_key_file": "",
  "allocator_url": "http://127.0.0.1:9000/proxies",
  "allocator_timeout_seconds": 5,
  "default_policy": {
    "min_ttl_seconds": 60,
    "min_remaining_qps": 2,
    "min_max_qps": 5
  },
  "bootstrap_proxies": [
    {
      "id": "p-1",
      "ip": "203.0.113.10",
      "port": 3128,
      "username": "upuser",
      "password": "uppass",
      "max_qps": 20,
      "expires_at": "2030-01-01T00:00:00Z"
    }
  ]
}
```

Allocator API response format:

```json
{
  "proxies": [
    {
      "id": "p-2",
      "ip": "203.0.113.11",
      "port": 3128,
      "username": "upuser",
      "password": "uppass",
      "max_qps": 20,
      "expires_at": "2030-01-01T00:00:00Z"
    }
  ]
}
```

## Run (example)

```bash
cd proxygateway

go run ./example -config config.json
```

Clients should send `Proxy-Authorization: Basic` with `user:password` or `user:password:channel`.

## User Lookup
The library expects a `GetUserFunc` callback. See `proxygateway/example/main.go` for an in-memory map example.
