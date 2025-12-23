package proxygateway

import "context"

type AllocatorFunc func(ctx context.Context) ([]ProxyInfo, error)
