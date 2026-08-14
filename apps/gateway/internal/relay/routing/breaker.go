package routing

import (
	"sync"
	"time"
)

// channelOutcome records a single upstream call outcome for breaker accounting.
type channelOutcome struct {
	at     time.Time
	failed bool
}

type channelBreakerState struct {
	outcomes  []channelOutcome
	openUntil time.Time
}

// CircuitBreakerConfig configures the per-channel breaker.
// 默认窗口 30s、最小样本 10、50% 失败阈值、熔断 60s（半开放探测）。
type CircuitBreakerConfig struct {
	Window        time.Duration
	OpenDuration  time.Duration
	MinimumSample int
	FailureRate   float64 // 0~1，失败次数占比阈值
}

// DefaultCircuitBreakerConfig returns the design-spec defaults.
func DefaultCircuitBreakerConfig() CircuitBreakerConfig {
	return CircuitBreakerConfig{
		Window:        30 * time.Second,
		OpenDuration:  60 * time.Second,
		MinimumSample: 10,
		FailureRate:   0.5,
	}
}

// CircuitBreaker tracks per-channel failure rates in a sliding window
// and opens (blocks requests) when the failure rate exceeds 50%.
type CircuitBreaker struct {
	mu     sync.Mutex
	states map[string]channelBreakerState
	now    func() time.Time
	cfg    CircuitBreakerConfig
}

// NewCircuitBreaker creates a breaker with the default config.
func NewCircuitBreaker() *CircuitBreaker {
	return NewCircuitBreakerWithConfig(DefaultCircuitBreakerConfig())
}

// NewCircuitBreakerWithConfig creates a breaker with explicit config.
func NewCircuitBreakerWithConfig(cfg CircuitBreakerConfig) *CircuitBreaker {
	if cfg.Window <= 0 || cfg.OpenDuration <= 0 || cfg.MinimumSample <= 0 || cfg.FailureRate <= 0 {
		cfg = DefaultCircuitBreakerConfig()
	}
	return &CircuitBreaker{
		states: make(map[string]channelBreakerState),
		now:    time.Now,
		cfg:    cfg,
	}
}

// SetNow injects a custom clock (for tests).
func (b *CircuitBreaker) SetNow(now func() time.Time) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.now = now
}

// Allow returns false if the channel is currently circuit-broken.
// 半开放状态：熔断时间到后允许探测请求，但状态保留直到下次 Record 决定。
func (b *CircuitBreaker) Allow(channelID string) bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	now := b.now()
	state := b.states[channelID]
	if state.openUntil.After(now) {
		return false
	}
	if !state.openUntil.IsZero() {
		state = channelBreakerState{}
		b.states[channelID] = state
	}
	return true
}

// Record updates the breaker with a single outcome.
// 窗口样本 >= MinimumSample 且失败率 > FailureRate 时打开熔断 OpenDuration。
func (b *CircuitBreaker) Record(channelID string, failed bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	now := b.now()
	state := b.states[channelID]
	cutoff := now.Add(-b.cfg.Window)
	kept := state.outcomes[:0]
	for _, outcome := range state.outcomes {
		if outcome.at.After(cutoff) {
			kept = append(kept, outcome)
		}
	}
	state.outcomes = append(kept, channelOutcome{at: now, failed: failed})
	if len(state.outcomes) >= b.cfg.MinimumSample {
		failures := 0
		for _, outcome := range state.outcomes {
			if outcome.failed {
				failures++
			}
		}
		if float64(failures) > b.cfg.FailureRate*float64(len(state.outcomes)) {
			state.openUntil = now.Add(b.cfg.OpenDuration)
		}
	}
	b.states[channelID] = state
}

// IsOpen returns true if the channel is currently circuit-broken (read-only).
func (b *CircuitBreaker) IsOpen(channelID string) bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	state := b.states[channelID]
	return state.openUntil.After(b.now())
}

// Reset clears the breaker state for a channel (for tests).
func (b *CircuitBreaker) Reset(channelID string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	delete(b.states, channelID)
}
