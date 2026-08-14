package server

import (
	"sync"
	"time"
)

type channelOutcome struct {
	at     time.Time
	failed bool
}

type channelBreakerState struct {
	outcomes  []channelOutcome
	openUntil time.Time
}

type CircuitBreaker struct {
	mu            sync.Mutex
	states        map[string]channelBreakerState
	now           func() time.Time
	window        time.Duration
	openDuration  time.Duration
	minimumSample int
}

func NewCircuitBreaker() *CircuitBreaker {
	return &CircuitBreaker{
		states:        make(map[string]channelBreakerState),
		now:           time.Now,
		window:        30 * time.Second,
		openDuration:  60 * time.Second,
		minimumSample: 10,
	}
}

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

func (b *CircuitBreaker) Record(channelID string, failed bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	now := b.now()
	state := b.states[channelID]
	cutoff := now.Add(-b.window)
	kept := state.outcomes[:0]
	for _, outcome := range state.outcomes {
		if outcome.at.After(cutoff) {
			kept = append(kept, outcome)
		}
	}
	state.outcomes = append(kept, channelOutcome{at: now, failed: failed})
	if len(state.outcomes) >= b.minimumSample {
		failures := 0
		for _, outcome := range state.outcomes {
			if outcome.failed {
				failures++
			}
		}
		if failures*2 > len(state.outcomes) {
			state.openUntil = now.Add(b.openDuration)
		}
	}
	b.states[channelID] = state
}
