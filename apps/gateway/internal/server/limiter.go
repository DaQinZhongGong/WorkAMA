package server

import (
	"sync"
	"time"
)

type rateWindow struct {
	started time.Time
	count   int
}

type Limiter struct {
	mu      sync.Mutex
	windows map[string]rateWindow
	now     func() time.Time
}

func NewLimiter() *Limiter {
	return &Limiter{windows: make(map[string]rateWindow), now: time.Now}
}

func (l *Limiter) Allow(key string, limit int) bool {
	if limit <= 0 {
		return false
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	now := l.now()
	window := l.windows[key]
	if window.started.IsZero() || now.Sub(window.started) >= time.Minute {
		l.windows[key] = rateWindow{started: now, count: 1}
		return true
	}
	if window.count >= limit {
		return false
	}
	window.count++
	l.windows[key] = window
	return true
}
