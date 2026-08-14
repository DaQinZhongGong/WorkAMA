package server

import (
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// TestLimiterAllowsFirstRequestInNewWindow verifies that the first request
// for a key creates a new window and is allowed.
func TestLimiterAllowsFirstRequestInNewWindow(t *testing.T) {
	limiter := NewLimiter()
	now := time.Unix(1000, 0)
	limiter.now = func() time.Time { return now }

	if !limiter.Allow("tenant_a", 10) {
		t.Fatal("first request in a new window should be allowed")
	}
}

// TestLimiterEnforcesLimitWithinWindow verifies that requests beyond the
// configured limit are rejected within the same one-minute window.
func TestLimiterEnforcesLimitWithinWindow(t *testing.T) {
	limiter := NewLimiter()
	now := time.Unix(2000, 0)
	limiter.now = func() time.Time { return now }

	limit := 5
	for i := 0; i < limit; i++ {
		if !limiter.Allow("tenant_b", limit) {
			t.Fatalf("request %d within limit should be allowed", i+1)
		}
	}
	if limiter.Allow("tenant_b", limit) {
		t.Fatal("request exceeding limit should be rejected")
	}
}

// TestLimiterRejectsNonPositiveLimit verifies the boundary case where
// limit is zero or negative — all requests must be rejected.
func TestLimiterRejectsNonPositiveLimit(t *testing.T) {
	limiter := NewLimiter()
	limiter.now = func() time.Time { return time.Unix(3000, 0) }

	for _, limit := range []int{0, -1, -100} {
		if limiter.Allow("tenant_c", limit) {
			t.Fatalf("limit %d should reject all requests", limit)
		}
	}
}

// TestLimiterTracksKeysIndependently verifies that different keys maintain
// independent rate windows.
func TestLimiterTracksKeysIndependently(t *testing.T) {
	limiter := NewLimiter()
	now := time.Unix(4000, 0)
	limiter.now = func() time.Time { return now }

	// Exhaust the limit for key_a.
	if !limiter.Allow("key_a", 1) {
		t.Fatal("first request for key_a should be allowed")
	}
	if limiter.Allow("key_a", 1) {
		t.Fatal("second request for key_a should be rejected")
	}

	// key_b should still have its full quota.
	if !limiter.Allow("key_b", 1) {
		t.Fatal("first request for key_b should be allowed independently")
	}
}

// TestLimiterWindowBoundaryReset verifies the exact boundary at which
// a window resets (>= 1 minute since the window started).
func TestLimiterWindowBoundaryReset(t *testing.T) {
	limiter := NewLimiter()
	now := time.Unix(5000, 0)
	limiter.now = func() time.Time { return now }

	// Start a window and exhaust the limit.
	if !limiter.Allow("boundary_key", 1) {
		t.Fatal("first request should be allowed")
	}
	if limiter.Allow("boundary_key", 1) {
		t.Fatal("second request should be rejected")
	}

	// Just under one minute — still in the same window.
	now = now.Add(59 * time.Second)
	if limiter.Allow("boundary_key", 1) {
		t.Fatal("request at 59s should still be rejected (same window)")
	}

	// Exactly one minute — window resets.
	now = now.Add(time.Second)
	if !limiter.Allow("boundary_key", 1) {
		t.Fatal("request at exactly 60s should start a new window")
	}
}

// TestLimiterConcurrentAccess verifies that Allow is safe under concurrent
// use and never exceeds the configured limit.
func TestLimiterConcurrentAccess(t *testing.T) {
	limiter := NewLimiter()
	limiter.now = func() time.Time { return time.Unix(6000, 0) }

	limit := 100
	var allowed int64
	var wg sync.WaitGroup
	for i := 0; i < 500; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if limiter.Allow("concurrent_key", limit) {
				atomic.AddInt64(&allowed, 1)
			}
		}()
	}
	wg.Wait()

	if int(allowed) != limit {
		t.Fatalf("concurrent allowed = %d, want exactly %d", allowed, limit)
	}
}

// TestLimiterNewWindowResetsCount verifies that after a window reset,
// the count starts fresh and the full limit is available again.
func TestLimiterNewWindowResetsCount(t *testing.T) {
	limiter := NewLimiter()
	now := time.Unix(7000, 0)
	limiter.now = func() time.Time { return now }

	limit := 3
	// Exhaust the window.
	for i := 0; i < limit; i++ {
		if !limiter.Allow("reset_key", limit) {
			t.Fatalf("request %d should be allowed", i+1)
		}
	}
	if limiter.Allow("reset_key", limit) {
		t.Fatal("request beyond limit should be rejected")
	}

	// Advance past the window.
	now = now.Add(time.Minute + time.Second)

	// After reset, the full limit should be available again.
	for i := 0; i < limit; i++ {
		if !limiter.Allow("reset_key", limit) {
			t.Fatalf("request %d after reset should be allowed", i+1)
		}
	}
	if limiter.Allow("reset_key", limit) {
		t.Fatal("request beyond limit after reset should be rejected")
	}
}

// TestLimiterUsesInjectedClock verifies that the now field is consulted
// for every Allow decision, not just the first one.
func TestLimiterUsesInjectedClock(t *testing.T) {
	limiter := NewLimiter()
	current := time.Unix(8000, 0)
	limiter.now = func() time.Time { return current }

	if !limiter.Allow("clock_key", 2) {
		t.Fatal("first request should be allowed")
	}
	if !limiter.Allow("clock_key", 2) {
		t.Fatal("second request should be allowed")
	}
	if limiter.Allow("clock_key", 2) {
		t.Fatal("third request should be rejected")
	}

	// Advance time but stay within the window.
	current = current.Add(30 * time.Second)
	if limiter.Allow("clock_key", 2) {
		t.Fatal("request at 30s should still be rejected (same window)")
	}

	// Advance past the window.
	current = current.Add(31 * time.Second)
	if !limiter.Allow("clock_key", 2) {
		t.Fatal("request after 61s should start a new window")
	}
}

// TestLimiterLargeLimitHandlesCorrectly verifies that the limiter
// correctly handles a large limit without overflow or off-by-one errors.
func TestLimiterLargeLimitHandlesCorrectly(t *testing.T) {
	limiter := NewLimiter()
	limiter.now = func() time.Time { return time.Unix(9000, 0) }

	limit := 1000
	for i := 0; i < limit; i++ {
		if !limiter.Allow("large_key", limit) {
			t.Fatalf("request %d should be allowed within limit %d", i+1, limit)
		}
	}
	if limiter.Allow("large_key", limit) {
		t.Fatal("request at limit+1 should be rejected")
	}
}

// TestLimiterEmptyKeyIsHandled verifies that an empty string key is
// treated like any other key.
func TestLimiterEmptyKeyIsHandled(t *testing.T) {
	limiter := NewLimiter()
	limiter.now = func() time.Time { return time.Unix(9100, 0) }

	if !limiter.Allow("", 2) {
		t.Fatal("first request with empty key should be allowed")
	}
	if !limiter.Allow("", 2) {
		t.Fatal("second request with empty key should be allowed")
	}
	if limiter.Allow("", 2) {
		t.Fatal("third request with empty key should be rejected")
	}
}
