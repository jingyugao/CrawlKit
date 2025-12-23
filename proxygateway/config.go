package proxygateway

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"time"
)

type Config struct {
	ListenAddr       string                 `json:"listen_addr"`
	TLSCertFile      string                 `json:"tls_cert_file"`
	TLSKeyFile       string                 `json:"tls_key_file"`
	AllocatorURL     string                 `json:"allocator_url"`
	AllocatorTimeout DurationSeconds         `json:"allocator_timeout_seconds"`
	DefaultPolicy    UserPolicy             `json:"default_policy"`
	BootstrapProxies []ProxyInfo            `json:"bootstrap_proxies"`
	Extra            map[string]interface{} `json:"-"`
}

type DurationSeconds struct {
	time.Duration
}

func (d *DurationSeconds) UnmarshalJSON(b []byte) error {
	var raw int
	if err := json.Unmarshal(b, &raw); err != nil {
		return err
	}
	if raw < 0 {
		return fmt.Errorf("duration must be non-negative")
	}
	d.Duration = time.Duration(raw) * time.Second
	return nil
}

type UserConfig struct {
	Password string     `json:"password"`
	Policy   UserPolicy `json:"policy"`
}

type UserPolicy struct {
	MinTTLSeconds     int `json:"min_ttl_seconds"`
	MinRemainingQPS   int `json:"min_remaining_qps"`
	MinMaxQPSRequired int `json:"min_max_qps"`
}

func LoadConfig(path string) (Config, error) {
	var cfg Config
	data, err := os.ReadFile(path)
	if err != nil {
		return cfg, err
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&cfg); err != nil {
		return cfg, err
	}
	if cfg.ListenAddr == "" {
		cfg.ListenAddr = ":8443"
	}
	if cfg.AllocatorTimeout.Duration == 0 {
		cfg.AllocatorTimeout.Duration = 5 * time.Second
	}
	return cfg, nil
}
