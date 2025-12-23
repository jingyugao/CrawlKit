package proxygateway

type Config struct {
	ListenAddr       string                 `json:"listen_addr"`
	TLSCertFile      string                 `json:"tls_cert_file"`
	TLSKeyFile       string                 `json:"tls_key_file"`
	BootstrapProxies []ProxyInfo            `json:"bootstrap_proxies"`
	Extra            map[string]interface{} `json:"-"`
}

type UserConfig struct {
	Password string     `json:"password"`
	Policy   UserPolicy `json:"policy"`
}

type UserPolicy struct {
	MinTTLSeconds     int `json:"min_ttl_seconds"`
	MinRemainingQPS   int `json:"min_remaining_qps"`
}
