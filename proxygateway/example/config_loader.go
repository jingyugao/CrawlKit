package main

import (
	"bytes"
	"encoding/json"
	"os"

	"proxygateway"
)

func LoadConfig(path string) (proxygateway.Config, error) {
	var cfg proxygateway.Config
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
	return cfg, nil
}
