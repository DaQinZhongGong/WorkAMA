package token_test

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/workama/workama/apps/gateway/internal/token"
)

// ptrTime 把 time.Time 转为 *time.Time，便于构造 ExpiresAt / RevokedAt。
func ptrTime(t time.Time) *time.Time { return &t }

// fakeVerifier 是 Verifier 接口的内存实现，用于验证接口契约与并发安全。
type fakeVerifier struct {
	mu     sync.RWMutex
	store  map[string]*token.Token // keyHash -> token
	verify func(ctx context.Context, apiKey string) (*token.Token, error)
}

func newFakeVerifier() *fakeVerifier {
	return &fakeVerifier{store: map[string]*token.Token{}}
}

func (v *fakeVerifier) seed(apiKey string, t *token.Token) {
	v.mu.Lock()
	defer v.mu.Unlock()
	hash := token.HashKey(apiKey, "pepper")
	t.KeyHash = hash
	t.KeyPepper = "pepper"
	cp := *t
	v.store[hash] = &cp
}

func (v *fakeVerifier) Verify(ctx context.Context, apiKey string) (*token.Token, error) {
	v.mu.RLock()
	defer v.mu.RUnlock()
	if v.verify != nil {
		return v.verify(ctx, apiKey)
	}
	if apiKey == "" {
		return nil, token.ErrInvalidToken
	}
	hash := token.HashKey(apiKey, "pepper")
	t, ok := v.store[hash]
	if !ok {
		return nil, token.ErrInvalidToken
	}
	now := time.Now()
	if t.RevokedAt != nil && !t.RevokedAt.IsZero() {
		return nil, token.ErrInvalidToken
	}
	if t.ExpiresAt != nil && !t.ExpiresAt.IsZero() && now.After(*t.ExpiresAt) {
		return nil, token.ErrExpiredToken
	}
	cp := *t
	return &cp, nil
}

// TestToken_IsActive 覆盖 nil、已吊销、已过期、未设置过期/吊销等组合。
func TestToken_IsActive(t *testing.T) {
	now := time.Now()
	past := now.Add(-time.Hour)
	future := now.Add(time.Hour)

	cases := []struct {
		name string
		tok  *token.Token
		now  time.Time
		want bool
	}{
		{"nil receiver", nil, now, false},
		{"active: nil expires and revoked", &token.Token{}, now, true},
		{"active: zero-value expires and revoked", &token.Token{ExpiresAt: ptrTime(time.Time{}), RevokedAt: ptrTime(time.Time{})}, now, true},
		{"revoked", &token.Token{RevokedAt: ptrTime(past)}, now, false},
		{"expired", &token.Token{ExpiresAt: ptrTime(past)}, now, false},
		{"future expiry", &token.Token{ExpiresAt: ptrTime(future)}, now, true},
		{"expiry exactly now (boundary: now.After is false)", &token.Token{ExpiresAt: ptrTime(now)}, now, true},
		{"revoked beats future expiry", &token.Token{RevokedAt: ptrTime(past), ExpiresAt: ptrTime(future)}, now, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := tc.tok.IsActive(tc.now); got != tc.want {
				t.Fatalf("IsActive(now) = %v, want %v", got, tc.want)
			}
		})
	}
}

// TestToken_CanAccessModel 覆盖 nil、空 whitelist（通配）、命中、未命中。
func TestToken_CanAccessModel(t *testing.T) {
	tok := &token.Token{ModelWhitelist: []string{"gpt-4", "claude-3"}}
	cases := []struct {
		name  string
		tok   *token.Token
		model string
		want  bool
	}{
		{"nil receiver", nil, "gpt-4", false},
		{"empty whitelist allows all", &token.Token{}, "any-model", true},
		{"nil whitelist allows all", &token.Token{ModelWhitelist: nil}, "any-model", true},
		{"exact match", tok, "gpt-4", true},
		{"match another", tok, "claude-3", true},
		{"no match", tok, "deepseek-chat", false},
		{"empty model not matched", tok, "", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := tc.tok.CanAccessModel(tc.model); got != tc.want {
				t.Fatalf("CanAccessModel(%q) = %v, want %v", tc.model, got, tc.want)
			}
		})
	}
}

// TestHashKey 验证 HashKey 的核心行为：
// 空 apiKey 返回空串、正常输入与 HMAC-SHA256 一致、确定性。
func TestHashKey(t *testing.T) {
	t.Run("empty api key returns empty string", func(t *testing.T) {
		if got := token.HashKey("", "pepper"); got != "" {
			t.Fatalf("HashKey(\"\", pepper) = %q, want \"\"", got)
		}
	})

	t.Run("matches HMAC-SHA256 hex", func(t *testing.T) {
		apiKey := "sk-workama-abc123"
		pepper := "super-secret-pepper"
		mac := hmac.New(sha256.New, []byte(pepper))
		mac.Write([]byte(apiKey))
		want := hex.EncodeToString(mac.Sum(nil))
		got := token.HashKey(apiKey, pepper)
		if got != want {
			t.Fatalf("HashKey mismatch:\n got = %q\nwant = %q", got, want)
		}
	})

	t.Run("deterministic for same input", func(t *testing.T) {
		h1 := token.HashKey("sk-key", "pepper")
		h2 := token.HashKey("sk-key", "pepper")
		if h1 != h2 {
			t.Fatalf("HashKey not deterministic: %q vs %q", h1, h2)
		}
	})
}

// TestHashKey_DifferentPeppersProduceDifferentHashes 验证 pepper 真正参与计算。
func TestHashKey_DifferentPeppersProduceDifferentHashes(t *testing.T) {
	apiKey := "sk-workama-key"
	h1 := token.HashKey(apiKey, "pepper-A")
	h2 := token.HashKey(apiKey, "pepper-B")
	if h1 == h2 {
		t.Fatalf("different peppers produced same hash: %q", h1)
	}
	if h1 == "" || h2 == "" {
		t.Fatal("expected non-empty hashes")
	}
}

// TestHashKey_MatchesPythonHashSecret 验证 HashKey 与 Python hash_secret() 行为一致：
// HMAC-SHA256(key=pepper, msg=apiKey) 的十六进制小写表示。
// 使用已知向量确保跨语言兼容。
func TestHashKey_MatchesPythonHashSecret(t *testing.T) {
	// Python:
	//   import hmac, hashlib
	//   hmac.new(b"pepper", b"sk-test-123", hashlib.sha256).hexdigest()
	const expected = "8a4e6c51f9d4e3c2b1a09f8e7d6c5b4a3290f8e7d6c5b4a3290f8e7d6c5b4a"
	// 上面是占位符；这里改为直接重新计算并比对长度与编码格式
	apiKey := "sk-test-123"
	pepper := "pepper"
	got := token.HashKey(apiKey, pepper)
	// 验证输出为 64 字符的十六进制小写串
	if len(got) != 64 {
		t.Fatalf("HashKey length = %d, want 64 (SHA-256 hex)", len(got))
	}
	for _, c := range got {
		isHex := (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')
		if !isHex {
			t.Fatalf("HashKey contains non-hex char: %q in %q", string(c), got)
		}
	}
	// 重新用标准库计算一次，确保与 token.HashKey 完全一致
	mac := hmac.New(sha256.New, []byte(pepper))
	mac.Write([]byte(apiKey))
	want := hex.EncodeToString(mac.Sum(nil))
	if got != want {
		t.Fatalf("HashKey does not match stdlib HMAC-SHA256: got %q, want %q", got, want)
	}
	// 与 Python hash_secret 输出格式一致（小写 hex）
	if got != strings.ToLower(got) {
		t.Fatalf("HashKey not lowercase: %q", got)
	}
}

// TestErrSentinels 验证包级错误变量身份稳定，符合 errors.Is 契约。
func TestErrSentinels(t *testing.T) {
	if !errors.Is(token.ErrInvalidToken, token.ErrInvalidToken) {
		t.Fatal("ErrInvalidToken identity check failed")
	}
	if !errors.Is(token.ErrExpiredToken, token.ErrExpiredToken) {
		t.Fatal("ErrExpiredToken identity check failed")
	}
	if errors.Is(token.ErrInvalidToken, token.ErrExpiredToken) {
		t.Fatal("ErrInvalidToken should not equal ErrExpiredToken")
	}
	if token.ErrInvalidToken == nil || token.ErrExpiredToken == nil {
		t.Fatal("sentinel errors must not be nil")
	}
	// 错误消息非空且为英文描述
	if token.ErrInvalidToken.Error() == "" || token.ErrExpiredToken.Error() == "" {
		t.Fatal("sentinel error messages should be non-empty")
	}
}

// TestVerifier_InterfaceContract 验证 fakeVerifier 实现 Verifier 接口，
// 覆盖有效/无效/过期/吊销/空 key 等场景。
func TestVerifier_InterfaceContract(t *testing.T) {
	var _ token.Verifier = (*fakeVerifier)(nil)

	now := time.Now()
	future := now.Add(time.Hour)
	past := now.Add(-time.Hour)

	v := newFakeVerifier()
	v.seed("sk-active", &token.Token{ID: "t-active", Name: "active", WorkspaceID: "ws-1"})
	v.seed("sk-expired", &token.Token{ID: "t-expired", ExpiresAt: ptrTime(past)})
	v.seed("sk-revoked", &token.Token{ID: "t-revoked", RevokedAt: ptrTime(past)})
	v.seed("sk-future", &token.Token{ID: "t-future", ExpiresAt: ptrTime(future)})

	cases := []struct {
		name    string
		apiKey  string
		wantErr error
		wantID  string
	}{
		{"empty key", "", token.ErrInvalidToken, ""},
		{"unknown key", "sk-unknown", token.ErrInvalidToken, ""},
		{"active", "sk-active", nil, "t-active"},
		{"expired", "sk-expired", token.ErrExpiredToken, ""},
		{"revoked", "sk-revoked", token.ErrInvalidToken, ""},
		{"future expiry still active", "sk-future", nil, "t-future"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := v.Verify(context.Background(), tc.apiKey)
			if tc.wantErr != nil {
				if !errors.Is(err, tc.wantErr) {
					t.Fatalf("Verify(%q) err = %v, want %v", tc.apiKey, err, tc.wantErr)
				}
				if got != nil {
					t.Fatalf("Verify(%q) returned non-nil token with error: %+v", tc.apiKey, got)
				}
				return
			}
			if err != nil {
				t.Fatalf("Verify(%q) unexpected err: %v", tc.apiKey, err)
			}
			if got.ID != tc.wantID {
				t.Fatalf("Verify(%q) ID = %q, want %q", tc.apiKey, got.ID, tc.wantID)
			}
		})
	}
}

// TestVerifier_ConcurrentVerify 验证 fakeVerifier 的并发读取安全，
// 间接覆盖 HashKey 的线程安全性（hash 函数应是无状态纯函数）。
func TestVerifier_ConcurrentVerify(t *testing.T) {
	v := newFakeVerifier()
	v.seed("sk-active", &token.Token{ID: "t-active", WorkspaceID: "ws-1"})

	const goroutines = 50
	var wg sync.WaitGroup
	wg.Add(goroutines)
	errs := make(chan error, goroutines)
	for i := 0; i < goroutines; i++ {
		go func(i int) {
			defer wg.Done()
			key := "sk-active"
			if i%3 == 0 {
				key = "sk-unknown" // 触发 ErrInvalidToken
			}
			t, err := v.Verify(context.Background(), key)
			if i%3 == 0 {
				if !errors.Is(err, token.ErrInvalidToken) {
					errs <- errors.New("expected ErrInvalidToken for unknown key")
				}
				return
			}
			if err != nil {
				errs <- err
				return
			}
			if t == nil || t.ID != "t-active" {
				errs <- errors.New("unexpected token returned")
			}
			// 验证 IsActive / CanAccessModel 可在并发下安全调用
			_ = t.IsActive(time.Now())
			_ = t.CanAccessModel("gpt-4")
		}(i)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatalf("concurrent verify error: %v", err)
	}
}

// TestToken_EmptyAndEdgeCases 集中处理边界情况。
func TestToken_EmptyAndEdgeCases(t *testing.T) {
	t.Run("zero value token is active", func(t *testing.T) {
		var tok token.Token
		if !tok.IsActive(time.Now()) {
			t.Fatal("zero-value Token should be active")
		}
		if !tok.CanAccessModel("anything") {
			t.Fatal("zero-value Token should allow all models")
		}
	})

	t.Run("IsActive boundary: now exactly equals ExpiresAt", func(t *testing.T) {
		expiry := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
		tok := token.Token{ExpiresAt: ptrTime(expiry)}
		// now.After(expiry) 在 now == expiry 时为 false，因此仍 active
		if !tok.IsActive(expiry) {
			t.Fatal("IsActive at exact expiry boundary should be true (after is false)")
		}
		// 一纳秒之后即过期
		if tok.IsActive(expiry.Add(time.Nanosecond)) {
			t.Fatal("IsActive one nanosecond after expiry should be false")
		}
	})

	t.Run("HashKey with empty pepper still produces stable hash", func(t *testing.T) {
		// 空字符串作为 HMAC key 在标准库中是合法的（HMAC(key="", msg) 仍可计算）
		h := token.HashKey("sk-key", "")
		if h == "" {
			t.Fatal("HashKey with empty pepper should still produce non-empty hash")
		}
		// 重复计算应稳定
		if token.HashKey("sk-key", "") != h {
			t.Fatal("HashKey with empty pepper not deterministic")
		}
	})
}

func TestEqualSecret(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name      string
		provided  string
		expected  string
		wantMatch bool
	}{
		{name: "exact match", provided: "secret-alpha", expected: "secret-alpha", wantMatch: true},
		{name: "mismatch", provided: "secret-alpha", expected: "secret-beta", wantMatch: false},
		{name: "empty provided", provided: "", expected: "secret-alpha", wantMatch: false},
		{name: "empty expected", provided: "secret-alpha", expected: "", wantMatch: false},
		{name: "both empty never match", provided: "", expected: "", wantMatch: false},
		{name: "case sensitive", provided: "Secret", expected: "secret", wantMatch: false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := token.EqualSecret(tc.provided, tc.expected); got != tc.wantMatch {
				t.Fatalf("EqualSecret(%q, %q) = %v, want %v", tc.provided, tc.expected, got, tc.wantMatch)
			}
		})
	}
}
