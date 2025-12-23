package main

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/gorilla/websocket"
	"github.com/shirou/gopsutil/v3/cpu"
	"github.com/shirou/gopsutil/v3/mem"
)

type statsSnapshot struct {
	CPUPercent   float64   `json:"cpu_percent"`
	MemTotal     uint64    `json:"mem_total_bytes"`
	MemUsed      uint64    `json:"mem_used_bytes"`
	MemPercent   float64   `json:"mem_used_percent"`
	CollectedAt  time.Time `json:"collected_at"`
	SamplePeriod string    `json:"sample_period"`
}

type statsStore struct {
	mu   sync.RWMutex
	last statsSnapshot
}

func (s *statsStore) set(snapshot statsSnapshot) {
	s.mu.Lock()
	s.last = snapshot
	s.mu.Unlock()
}

func (s *statsStore) get() statsSnapshot {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.last
}

type connPair struct {
	downstream *websocket.Conn
	upstream   *websocket.Conn
}

type proxyServer struct {
	upstreamURL *url.URL
	reverse     *httputil.ReverseProxy
	upgrader    websocket.Upgrader
	mu          sync.Mutex
	active      map[*connPair]struct{}
	closing     atomic.Bool
	wg          sync.WaitGroup
}

func newProxyServer(upstreamURL *url.URL) *proxyServer {
	reverse := httputil.NewSingleHostReverseProxy(upstreamURL)
	reverse.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		http.Error(w, "upstream error", http.StatusBadGateway)
	}
	return &proxyServer{
		upstreamURL: upstreamURL,
		reverse:     reverse,
		upgrader: websocket.Upgrader{
			CheckOrigin: func(r *http.Request) bool { return true },
		},
		active: make(map[*connPair]struct{}),
	}
}

func (p *proxyServer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if p.closing.Load() {
		http.Error(w, "shutting down", http.StatusServiceUnavailable)
		return
	}
	if isWebsocketRequest(r) {
		p.handleWebsocket(w, r)
		return
	}
	p.reverse.ServeHTTP(w, r)
}

func (p *proxyServer) handleWebsocket(w http.ResponseWriter, r *http.Request) {
	downstream, err := p.upgrader.Upgrade(w, r, nil)
	if err != nil {
		return
	}

	upstreamURL := *p.upstreamURL
	upstreamURL.Path = r.URL.Path
	upstreamURL.RawQuery = r.URL.RawQuery
	if upstreamURL.Scheme == "https" {
		upstreamURL.Scheme = "wss"
	} else {
		upstreamURL.Scheme = "ws"
	}

	headers := http.Header{}
	if protocols := r.Header.Get("Sec-WebSocket-Protocol"); protocols != "" {
		headers.Set("Sec-WebSocket-Protocol", protocols)
	}

	upstream, resp, err := websocket.DefaultDialer.Dial(upstreamURL.String(), headers)
	if err != nil {
		if resp != nil {
			resp.Body.Close()
		}
		_ = downstream.Close()
		return
	}

	pair := &connPair{downstream: downstream, upstream: upstream}
	p.addPair(pair)
	defer p.removePair(pair)

	errc := make(chan error, 2)
	go proxyWebsocket(upstream, downstream, errc)
	go proxyWebsocket(downstream, upstream, errc)

	<-errc
	pair.close("proxy closed")
}

func (p *proxyServer) addPair(pair *connPair) {
	p.mu.Lock()
	p.active[pair] = struct{}{}
	p.mu.Unlock()
	p.wg.Add(1)
}

func (p *proxyServer) removePair(pair *connPair) {
	p.mu.Lock()
	delete(p.active, pair)
	p.mu.Unlock()
	p.wg.Done()
}

func (p *proxyServer) initiateShutdown(reason string) {
	if p.closing.Swap(true) {
		return
	}
	p.mu.Lock()
	for pair := range p.active {
		pair.close(reason)
	}
	p.mu.Unlock()
}

func (p *proxyServer) waitForConnections(timeout time.Duration) bool {
	done := make(chan struct{})
	go func() {
		p.wg.Wait()
		close(done)
	}()
	select {
	case <-done:
		return true
	case <-time.After(timeout):
		return false
	}
}

func (p *connPair) close(reason string) {
	msg := websocket.FormatCloseMessage(websocket.CloseGoingAway, reason)
	_ = p.upstream.WriteControl(websocket.CloseMessage, msg, time.Now().Add(2*time.Second))
	_ = p.downstream.WriteControl(websocket.CloseMessage, msg, time.Now().Add(2*time.Second))
	_ = p.upstream.Close()
	_ = p.downstream.Close()
}

func proxyWebsocket(dst, src *websocket.Conn, errc chan<- error) {
	for {
		messageType, payload, err := src.ReadMessage()
		if err != nil {
			errc <- err
			return
		}
		if err := dst.WriteMessage(messageType, payload); err != nil {
			errc <- err
			return
		}
	}
}

func isWebsocketRequest(r *http.Request) bool {
	if !strings.EqualFold(r.Header.Get("Upgrade"), "websocket") {
		return false
	}
	connection := r.Header.Get("Connection")
	return strings.Contains(strings.ToLower(connection), "upgrade")
}

func startStatsSampler(ctx context.Context, store *statsStore, interval time.Duration) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			store.set(collectStats(interval))
		}
	}
}

func collectStats(interval time.Duration) statsSnapshot {
	cpuPercent := 0.0
	memTotal := uint64(0)
	memUsed := uint64(0)
	memPercent := 0.0

	if percents, err := cpu.Percent(0, false); err == nil && len(percents) > 0 {
		cpuPercent = percents[0]
	}
	if memInfo, err := mem.VirtualMemory(); err == nil {
		memTotal = memInfo.Total
		memUsed = memInfo.Used
		memPercent = memInfo.UsedPercent
	}

	return statsSnapshot{
		CPUPercent:   cpuPercent,
		MemTotal:     memTotal,
		MemUsed:      memUsed,
		MemPercent:   memPercent,
		CollectedAt:  time.Now().UTC(),
		SamplePeriod: interval.String(),
	}
}

func main() {
	listenAddr := getEnv("LISTEN_ADDR", ":9222")
	upstreamStr := getEnv("UPSTREAM_URL", "http://127.0.0.1:9220")
	sampleInterval := getEnvDuration("SAMPLE_INTERVAL", 5*time.Second)
	shutdownTimeout := getEnvDuration("SHUTDOWN_TIMEOUT", 15*time.Second)
	readyTimeout := getEnvDuration("READY_TIMEOUT", 2*time.Second)

	upstreamURL, err := url.Parse(upstreamStr)
	if err != nil {
		log.Fatalf("invalid UPSTREAM_URL: %v", err)
	}

	stats := &statsStore{
		last: collectStats(sampleInterval),
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go startStatsSampler(ctx, stats, sampleInterval)

	proxy := newProxyServer(upstreamURL)

	ready := atomic.Bool{}
	ready.Store(true)

	mux := http.NewServeMux()
	mux.Handle("/stats", statsHandler(stats))
	mux.Handle("/healthz", healthHandler())
	mux.Handle("/readyz", readinessHandler(&ready, upstreamURL, readyTimeout))
	mux.Handle("/", proxy)

	server := &http.Server{
		Addr:    listenAddr,
		Handler: mux,
		BaseContext: func(l net.Listener) context.Context {
			return ctx
		},
	}

	sigCtx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go func() {
		<-sigCtx.Done()
		ready.Store(false)
		proxy.initiateShutdown("k8s shutdown")
		shutdownCtx, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
		defer cancel()
		if err := server.Shutdown(shutdownCtx); err != nil && !errors.Is(err, context.Canceled) {
			log.Printf("http shutdown error: %v", err)
		}
	}()

	log.Printf("listening on %s, upstream %s", listenAddr, upstreamURL.String())
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("listen error: %v", err)
	}

	cancel()
	if ok := proxy.waitForConnections(shutdownTimeout); !ok {
		log.Printf("upstream connections did not close in time")
		os.Exit(2)
	}
}

func statsHandler(store *statsStore) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		snapshot := store.get()
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(snapshot)
	})
}

func healthHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
}

func readinessHandler(ready *atomic.Bool, upstreamURL *url.URL, timeout time.Duration) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !ready.Load() {
			http.Error(w, "not ready", http.StatusServiceUnavailable)
			return
		}

		ctx, cancel := context.WithTimeout(r.Context(), timeout)
		defer cancel()
		checkURL := *upstreamURL
		checkURL.Path = "/json/version"
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, checkURL.String(), nil)
		if err != nil {
			http.Error(w, "not ready", http.StatusServiceUnavailable)
			return
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			http.Error(w, "not ready", http.StatusServiceUnavailable)
			return
		}
		resp.Body.Close()
		if resp.StatusCode < http.StatusOK || resp.StatusCode >= http.StatusMultipleChoices {
			http.Error(w, "not ready", http.StatusServiceUnavailable)
			return
		}

		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ready"))
	})
}

func getEnv(key, fallback string) string {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	return value
}

func getEnvDuration(key string, fallback time.Duration) time.Duration {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	if parsed, err := time.ParseDuration(value); err == nil {
		return parsed
	}
	if secs, err := strconv.Atoi(value); err == nil {
		return time.Duration(secs) * time.Second
	}
	return fallback
}
