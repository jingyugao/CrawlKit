package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"proxygateway"
)

func main() {
	configPath := flag.String("config", "config.json", "path to config JSON")
	flag.Parse()

	cfg, err := LoadConfig(*configPath)
	if err != nil {
		log.Fatalf("load config: %v", err)
	}

	users := map[string]proxygateway.UserConfig{
		"alice": {
			Password: "secret",
			Policy: proxygateway.UserPolicy{
				MinTTLSeconds:     120,
				MinRemainingQPS:   3,
			},
		},
	}

	getUser := func(ctx context.Context, username string) (proxygateway.UserConfig, bool, error) {
		cfg, ok := users[username]
		return cfg, ok, nil
	}

	allocator := NewHTTPAllocator("http://127.0.0.1:9000/proxies", 5*time.Second)

	pool := proxygateway.NewProxyPool(allocator)
	if err := proxygateway.ValidateProxies(cfg.BootstrapProxies); err != nil && len(cfg.BootstrapProxies) > 0 {
		log.Fatalf("invalid bootstrap proxies: %v", err)
	}
	pool.AddBatch(cfg.BootstrapProxies)

	server := proxygateway.NewProxyServer(pool, proxygateway.UserPolicy{}, getUser, nil)
	httpServer := &http.Server{
		Addr:         cfg.ListenAddr,
		Handler:      server,
		ReadTimeout:  15 * time.Second,
		WriteTimeout: 15 * time.Second,
	}

	go func() {
		if cfg.TLSCertFile != "" && cfg.TLSKeyFile != "" {
			if err := httpServer.ListenAndServeTLS(cfg.TLSCertFile, cfg.TLSKeyFile); err != nil && err != http.ErrServerClosed {
				log.Printf("server error: %v", err)
			}
			return
		}
		if err := httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Printf("server error: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGTERM, syscall.SIGINT)
	<-stop

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = httpServer.Shutdown(ctx)
}
