package configsync

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// 测试用密钥对（由 Python cryptography.fernet 生成，保证字节级兼容性）：
// master key = cO7xNnSxpgP08Mwk-8QYpD30KjJrM5wTmb42wJn4E2c=
// token 加密明文 = "sk-test-e2e-9183"
const (
	testFernetKey   = "cO7xNnSxpgP08Mwk-8QYpD30KjJrM5wTmb42wJn4E2c="
	testSecretPlain = "sk-test-e2e-9183"
	testToken       = "gAAAAABqirmcUdpg3TIkm6kpbd569SutGA8GawZxymtzbqcGc0dRxPRxxKpVmS8agmqgbyP9lXbxE8Od2gIoFLzuSDaCgQp8vsrPoLrcxROr8r7r1hFfIbw="
)

func exportHandler(version int, withAuthCheck bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if withAuthCheck && r.Header.Get("X-Internal-Token") != "tok-1" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"version": version,
			"values": map[string]any{
				"llm_staging_enabled":  true,
				"llm_staging_provider": "openai-compatible",
				"rate_limit_default_per_min": 60,
			},
			"secrets": map[string]any{"llm_staging_api_key": testToken},
		})
	}
}

func TestFetchOnceDecryptsSecretsAndNormalizesValues(t *testing.T) {
	srv := httptest.NewServer(exportHandler(7, true))
	defer srv.Close()
	p := &Poller{Endpoint: srv.URL, Token: "tok-1", EncryptionKey: testFernetKey}
	snap, err := p.fetchOnce(context.Background())
	if err != nil {
		t.Fatalf("fetchOnce: %v", err)
	}
	if snap.Version != 7 {
		t.Fatalf("version = %d, want 7", snap.Version)
	}
	if snap.Value("llm_staging_enabled") != "true" {
		t.Fatalf("bool normalize failed: %q", snap.Value("llm_staging_enabled"))
	}
	if got := snap.Value("llm_staging_provider"); got != "openai-compatible" {
		t.Fatalf("provider = %q", got)
	}
	if got := snap.Secret("llm_staging_api_key"); got != testSecretPlain {
		t.Fatalf("secret decrypt mismatch: %q", got)
	}
}

func TestFetchOnceAuthRequired(t *testing.T) {
	srv := httptest.NewServer(exportHandler(1, true))
	defer srv.Close()
	p := &Poller{Endpoint: srv.URL, Token: "wrong"}
	if _, err := p.fetchOnce(context.Background()); err == nil {
		t.Fatal("expected auth error, got nil")
	}
}

func TestRunInvokesCallbackOnVersionChange(t *testing.T) {
	srv := httptest.NewServer(exportHandler(11, false))
	defer srv.Close()
	calls := make(chan *Snapshot, 4)
	p := &Poller{Endpoint: srv.URL, Token: "t", EncryptionKey: testFernetKey, Interval: 20 * time.Millisecond}
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()
	go func() { _ = p.Run(ctx, func(s *Snapshot) { calls <- s }) }()
	first := <-calls
	if first.Version != 11 {
		t.Fatalf("first callback version = %d", first.Version)
	}
	select {
	case extra := <-calls:
		t.Fatalf("unexpected duplicate callback for same version: %d", extra.Version)
	case <-time.After(120 * time.Millisecond):
		// 同 version 不重复回调 —— 符合预期
	}
}

func TestRunRecoversAfterServerFailure(t *testing.T) {
	var handler http.HandlerFunc = exportHandler(3, false)
	fail := true
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if fail {
			http.Error(w, "boom", http.StatusInternalServerError)
			return
		}
		handler(w, r)
	}))
	defer srv.Close()
	got := make(chan *Snapshot, 4)
	p := &Poller{Endpoint: srv.URL, Token: "t", Interval: 20 * time.Millisecond}
	ctx, cancel := context.WithTimeout(context.Background(), 1500*time.Millisecond)
	defer cancel()
	go func() { _ = p.Run(ctx, func(s *Snapshot) { got <- s }) }()

	time.Sleep(200 * time.Millisecond) // 处于失败退避窗口
	fail = false                       // 恢复后必须重新回调
	select {
	case s := <-got:
		if s.Version != 3 {
			t.Fatalf("recovered version = %d", s.Version)
		}
	case <-time.After(1200 * time.Millisecond):
		t.Fatal("no callback after recovery")
	}
}

func TestSecretSkippedWithoutEncryptionKey(t *testing.T) {
	srv := httptest.NewServer(exportHandler(5, false))
	defer srv.Close()
	p := &Poller{Endpoint: srv.URL, Token: "t"} // 无 EncryptionKey
	snap, err := p.fetchOnce(context.Background())
	if err != nil {
		t.Fatalf("fetchOnce: %v", err)
	}
	if len(snap.Secrets) != 0 {
		t.Fatalf("expected empty secrets without key, got %d", len(snap.Secrets))
	}
}
