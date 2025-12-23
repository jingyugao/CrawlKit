package proxygateway

import (
	"errors"
	"fmt"
	"sync"
	"time"
)

type ProxyInfo struct {
	ID        string    `json:"id"`
	IP        string    `json:"ip"`
	Port      int       `json:"port"`
	Username  string    `json:"username"`
	Password  string    `json:"password"`
	MaxQPS    int       `json:"max_qps"`
	ExpiresAt time.Time `json:"expires_at"`
}

type ProxyEntry struct {
	Info ProxyInfo
	QPS  *QPSCounter
}

type QPSCounter struct {
	mu     sync.Mutex
	sec    int64
	count  int
	maxQPS int
}

func NewQPSCounter(maxQPS int) *QPSCounter {
	return &QPSCounter{maxQPS: maxQPS}
}

func (q *QPSCounter) Remaining() int {
	q.mu.Lock()
	defer q.mu.Unlock()
	q.roll()
	return q.maxQPS - q.count
}

func (q *QPSCounter) TryUse(minRemaining int) bool {
	q.mu.Lock()
	defer q.mu.Unlock()
	q.roll()
	if q.maxQPS-q.count <= minRemaining {
		return false
	}
	q.count++
	return true
}

func (q *QPSCounter) roll() {
	sec := time.Now().Unix()
	if sec != q.sec {
		q.sec = sec
		q.count = 0
	}
}

type ProxyPool struct {
	mu        sync.Mutex
	entries   []ProxyEntry
	idx       int
	bindings  map[string]string
	allocator AllocatorFunc
}

func NewProxyPool(allocator AllocatorFunc) *ProxyPool {
	return &ProxyPool{
		entries:   make([]ProxyEntry, 0),
		bindings:  make(map[string]string),
		allocator: allocator,
	}
}

func (p *ProxyPool) Add(info ProxyInfo) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.entries = append(p.entries, ProxyEntry{Info: info, QPS: NewQPSCounter(info.MaxQPS)})
}

func (p *ProxyPool) AddBatch(infos []ProxyInfo) {
	for _, info := range infos {
		p.Add(info)
	}
}

func (p *ProxyPool) Select(policy UserPolicy, bindKey string) (ProxyEntry, error) {
	p.mu.Lock()
	defer p.mu.Unlock()

	if bindKey != "" {
		if boundID, ok := p.bindings[bindKey]; ok {
			if entry, ok := p.findByID(boundID); ok && isEligible(entry, policy) {
				if entry.QPS.TryUse(policy.MinRemainingQPS) {
					return entry, nil
				}
			}
			delete(p.bindings, bindKey)
		}
	}

	entry, ok := p.nextEligible(policy)
	if !ok {
		return ProxyEntry{}, errors.New("no available proxies")
	}
	if bindKey != "" {
		p.bindings[bindKey] = entry.Info.ID
	}
	return entry, nil
}

func (p *ProxyPool) nextEligible(policy UserPolicy) (ProxyEntry, bool) {
	p.prune()
	if len(p.entries) == 0 {
		return ProxyEntry{}, false
	}

	start := p.idx
	for i := 0; i < len(p.entries); i++ {
		idx := (start + i) % len(p.entries)
		entry := p.entries[idx]
		if !isEligible(entry, policy) {
			continue
		}
		if entry.QPS.TryUse(policy.MinRemainingQPS) {
			p.idx = (idx + 1) % len(p.entries)
			return entry, true
		}
	}
	return ProxyEntry{}, false
}

func (p *ProxyPool) prune() {
	if len(p.entries) == 0 {
		return
	}
	now := time.Now()
	filtered := p.entries[:0]
	for _, entry := range p.entries {
		if entry.Info.ExpiresAt.After(now) {
			filtered = append(filtered, entry)
		}
	}
	p.entries = filtered
}

func (p *ProxyPool) findByID(id string) (ProxyEntry, bool) {
	for _, entry := range p.entries {
		if entry.Info.ID == id {
			return entry, true
		}
	}
	return ProxyEntry{}, false
}

func isEligible(entry ProxyEntry, policy UserPolicy) bool {
	if entry.Info.MaxQPS < policy.MinMaxQPSRequired {
		return false
	}
	if policy.MinTTLSeconds > 0 {
		remaining := time.Until(entry.Info.ExpiresAt)
		if remaining < time.Duration(policy.MinTTLSeconds)*time.Second {
			return false
		}
	}
	return entry.QPS.Remaining() > policy.MinRemainingQPS
}

func EnsureIDs(infos []ProxyInfo) error {
	for i := range infos {
		if infos[i].ID == "" {
			infos[i].ID = fmt.Sprintf("proxy-%d-%d", time.Now().UnixNano(), i)
		}
		if infos[i].IP == "" || infos[i].Port == 0 {
			return errors.New("proxy info missing ip or port")
		}
		if infos[i].ExpiresAt.IsZero() {
			return errors.New("proxy info missing expires_at")
		}
	}
	return nil
}
