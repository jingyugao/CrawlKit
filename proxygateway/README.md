# proxygateway

A lightweight HTTPS tunnel proxy gateway that selects upstream proxies based on user policy.

## Features
- HTTPS CONNECT proxy server
- Per-user policy filters (TTL, remaining QPS)
- Optional channel binding (`user:password:channel`)
- Auto-fetch proxies from an allocator API when pool is empty

## Config
Create `config.json`:

```json
{
  "listen_addr": ":8443",
  "tls_cert_file": "",
  "tls_key_file": "",
  "bootstrap_proxies": [
    {
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

## Allocator Example
`proxygateway/example/allocator_http.go` shows a simple HTTP-based allocator implementation.
