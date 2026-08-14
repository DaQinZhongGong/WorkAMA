package server

import (
	"testing"
	"time"
)

func TestCircuitBreakerOpensAndRecovers(t *testing.T) {
	breaker := NewCircuitBreaker()
	now := time.Unix(1000, 0)
	breaker.now = func() time.Time { return now }
	for i := 0; i < 6; i++ {
		breaker.Record("channel", true)
	}
	for i := 0; i < 4; i++ {
		breaker.Record("channel", false)
	}
	if breaker.Allow("channel") {
		t.Fatal("breaker should be open above 50 percent failures")
	}
	now = now.Add(61 * time.Second)
	if !breaker.Allow("channel") {
		t.Fatal("breaker did not recover after the open interval")
	}
}

func TestCircuitBreakerRequiresMinimumSample(t *testing.T) {
	breaker := NewCircuitBreaker()
	for i := 0; i < 9; i++ {
		breaker.Record("channel", true)
	}
	if !breaker.Allow("channel") {
		t.Fatal("breaker opened before minimum sample size")
	}
}
