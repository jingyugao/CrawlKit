package proxygateway

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"
)

type AllocatorFunc func(ctx context.Context) ([]ProxyInfo, error)

func NewHTTPAllocator(url string, timeout time.Duration) AllocatorFunc {
	return func(ctx context.Context) ([]ProxyInfo, error) {
		if url == "" {
			return nil, fmt.Errorf("allocator_url is empty")
		}
		ctx, cancel := context.WithTimeout(ctx, timeout)
		defer cancel()
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
		if err != nil {
			return nil, err
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			return nil, err
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			return nil, fmt.Errorf("allocator status %s", resp.Status)
		}
		var payload struct {
			Proxies []ProxyInfo `json:"proxies"`
		}
		dec := json.NewDecoder(resp.Body)
		if err := dec.Decode(&payload); err != nil {
			return nil, err
		}
		if len(payload.Proxies) == 0 {
			return nil, fmt.Errorf("allocator returned no proxies")
		}
		return payload.Proxies, nil
	}
}
