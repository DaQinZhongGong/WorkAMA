package server

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"
)

type fakeHealthChecker struct {
	healthy bool
}

func (f *fakeHealthChecker) CheckHealth(ctx context.Context) error {
	if !f.healthy {
		return errors.New("db unavailable")
	}
	return nil
}

func TestHealthzReturns200WhenHealthy(t *testing.T) {
	srv := &Server{Logger: slog.Default(), HealthChecker: &fakeHealthChecker{healthy: true}}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	srv.health(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if !contains(rec.Body.String(), `"status":"ok"`) {
		t.Fatalf("expected ok status, got %s", rec.Body.String())
	}
}

func TestHealthzReturns503WhenUnhealthy(t *testing.T) {
	srv := &Server{Logger: slog.Default(), HealthChecker: &fakeHealthChecker{healthy: false}}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	srv.health(rec, req)
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("expected 503, got %d", rec.Code)
	}
	if !contains(rec.Body.String(), `"status":"unavailable"`) {
		t.Fatalf("expected unavailable status, got %s", rec.Body.String())
	}
}

func TestHealthzReturns200WhenNoChecker(t *testing.T) {
	srv := &Server{Logger: slog.Default()}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	srv.health(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
}

func contains(s, substr string) bool {
	return len(s) >= len(substr) && (s == substr || len(s) > 0 && containsHelper(s, substr))
}

func containsHelper(s, substr string) bool {
	for i := 0; i <= len(s)-len(substr); i++ {
		if s[i:i+len(substr)] == substr {
			return true
		}
	}
	return false
}
