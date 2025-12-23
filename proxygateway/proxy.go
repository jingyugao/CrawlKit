package proxygateway

import (
	"bufio"
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type ProxyServer struct {
	pool          *ProxyPool
	defaultPolicy UserPolicy
	getUser       GetUserFunc
	logger        *Logger
}

type GetUserFunc func(ctx context.Context, username string) (UserConfig, bool, error)

func NewProxyServer(pool *ProxyPool, defaultPolicy UserPolicy, getUser GetUserFunc, logger *Logger) *ProxyServer {
	if logger == nil {
		logger = NewLogger()
	}
	return &ProxyServer{
		pool:          pool,
		defaultPolicy: defaultPolicy,
		getUser:       getUser,
		logger:        logger,
	}
}

func (s *ProxyServer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodConnect {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	s.handleConnect(w, r)
}

func (s *ProxyServer) handleConnect(w http.ResponseWriter, r *http.Request) {
	user, policy, bindKey, err := s.authenticate(r)
	if err != nil {
		w.Header().Set("Proxy-Authenticate", "Basic")
		w.WriteHeader(http.StatusProxyAuthRequired)
		return
	}

	entry, err := s.selectProxy(r.Context(), policy, bindKey)
	if err != nil {
		s.logger.Errorf("no proxy for user %s: %v", user, err)
		w.WriteHeader(http.StatusServiceUnavailable)
		return
	}

	upstreamConn, err := net.DialTimeout("tcp", fmt.Sprintf("%s:%d", entry.Info.IP, entry.Info.Port), 8*time.Second)
	if err != nil {
		s.logger.Errorf("upstream dial failed: %v", err)
		w.WriteHeader(http.StatusBadGateway)
		return
	}
	defer upstreamConn.Close()

	if err := s.connectViaUpstream(upstreamConn, r.Host, entry.Info); err != nil {
		s.logger.Errorf("upstream connect failed: %v", err)
		w.WriteHeader(http.StatusBadGateway)
		return
	}

	clientConn, _, err := w.(http.Hijacker).Hijack()
	if err != nil {
		s.logger.Errorf("hijack failed: %v", err)
		return
	}
	defer clientConn.Close()

	_, _ = clientConn.Write([]byte("HTTP/1.1 200 Connection Established\r\n\r\n"))

	go pipe(upstreamConn, clientConn)
	pipe(clientConn, upstreamConn)
}

func (s *ProxyServer) connectViaUpstream(conn net.Conn, target string, info ProxyInfo) error {
	req := &http.Request{
		Method: http.MethodConnect,
		URL:    &url.URL{Opaque: target},
		Host:   target,
		Header: make(http.Header),
	}
	if info.Username != "" {
		payload := base64.StdEncoding.EncodeToString([]byte(info.Username + ":" + info.Password))
		req.Header.Set("Proxy-Authorization", "Basic "+payload)
	}
	req.Header.Set("Proxy-Connection", "Keep-Alive")
	if err := req.Write(conn); err != nil {
		return err
	}
	reader := bufio.NewReader(conn)
	resp, err := http.ReadResponse(reader, req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		return fmt.Errorf("upstream status %s: %s", resp.Status, strings.TrimSpace(string(body)))
	}
	return nil
}

func pipe(dst net.Conn, src net.Conn) {
	_, _ = io.Copy(dst, src)
}

func (s *ProxyServer) authenticate(r *http.Request) (string, UserPolicy, string, error) {
	user, pass, channel, err := parseProxyAuth(r)
	if err != nil {
		return "", UserPolicy{}, "", err
	}
	if s.getUser == nil {
		return "", UserPolicy{}, "", errors.New("get user function is nil")
	}
	cfg, ok, err := s.getUser(r.Context(), user)
	if err != nil {
		return "", UserPolicy{}, "", err
	}
	if !ok || cfg.Password != pass {
		return "", UserPolicy{}, "", errors.New("invalid credentials")
	}
	policy := cfg.Policy
	if policy.MinTTLSeconds == 0 {
		policy.MinTTLSeconds = s.defaultPolicy.MinTTLSeconds
	}
	if policy.MinRemainingQPS == 0 {
		policy.MinRemainingQPS = s.defaultPolicy.MinRemainingQPS
	}
	bindKey := ""
	if channel != "" {
		bindKey = user + ":" + channel
	}
	return user, policy, bindKey, nil
}

func (s *ProxyServer) selectProxy(ctx context.Context, policy UserPolicy, bindKey string) (ProxyEntry, error) {
	entry, err := s.pool.Select(policy, bindKey)
	if err == nil {
		return entry, nil
	}
	if s.pool.allocator == nil {
		return ProxyEntry{}, err
	}
	proxies, fetchErr := s.pool.allocator(ctx)
	if fetchErr != nil {
		return ProxyEntry{}, fetchErr
	}
	if err := ValidateProxies(proxies); err != nil {
		return ProxyEntry{}, err
	}
	s.pool.AddBatch(proxies)
	return s.pool.Select(policy, bindKey)
}

func parseProxyAuth(r *http.Request) (string, string, string, error) {
	value := r.Header.Get("Proxy-Authorization")
	if value == "" {
		return "", "", "", errors.New("missing proxy authorization")
	}
	parts := strings.Fields(value)
	if len(parts) != 2 || !strings.EqualFold(parts[0], "Basic") {
		return "", "", "", errors.New("unsupported auth")
	}
	decoded, err := base64.StdEncoding.DecodeString(parts[1])
	if err != nil {
		return "", "", "", errors.New("invalid auth encoding")
	}
	segments := strings.Split(string(decoded), ":")
	if len(segments) < 2 {
		return "", "", "", errors.New("invalid auth format")
	}
	user := segments[0]
	pass := segments[1]
	channel := ""
	if len(segments) >= 3 {
		channel = strings.Join(segments[2:], ":")
	}
	return user, pass, channel, nil
}
