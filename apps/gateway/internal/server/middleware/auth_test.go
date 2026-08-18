package middleware

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/workama/workama/apps/gateway/internal/token"
)

type stubVerifier struct {
	tok *token.Token
	err error
}

func (s stubVerifier) Verify(ctx context.Context, apiKey string) (*token.Token, error) {
	if s.err != nil {
		return nil, s.err
	}
	return s.tok, nil
}

func nextOK() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		tok := TokenFromContext(r.Context())
		if tok == nil {
			http.Error(w, "missing token", http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(map[string]string{
			"workspace_id": tok.WorkspaceID,
			"token_id":     tok.ID,
		})
	})
}

func TestAuthInternalTokenAccepted(t *testing.T) {
	mw := NewAuth(stubVerifier{}, "dev-internal-secret")
	req := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	req.Header.Set("X-Internal-Token", "dev-internal-secret")
	req.Header.Set("X-Workspace-ID", "wsp_test")
	rec := httptest.NewRecorder()

	mw.Wrap(nextOK()).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var body map[string]string
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body["workspace_id"] != "wsp_test" || body["token_id"] != "internal" {
		t.Fatalf("unexpected body: %#v", body)
	}
}

func TestAuthInternalTokenRejectedWhenWrong(t *testing.T) {
	mw := NewAuth(stubVerifier{}, "dev-internal-secret")
	req := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	req.Header.Set("X-Internal-Token", "forged-token")
	req.Header.Set("X-Workspace-ID", "wsp_test")
	rec := httptest.NewRecorder()

	mw.Wrap(nextOK()).ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401; body = %s", rec.Code, rec.Body.String())
	}
}

func TestAuthInternalTokenRejectedWhenWorkspaceMissing(t *testing.T) {
	mw := NewAuth(stubVerifier{}, "dev-internal-secret")
	req := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	req.Header.Set("X-Internal-Token", "dev-internal-secret")
	rec := httptest.NewRecorder()

	mw.Wrap(nextOK()).ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401; body = %s", rec.Code, rec.Body.String())
	}
	if !contains(rec.Body.String(), "Missing X-Workspace-ID") {
		t.Fatalf("expected workspace error, got %s", rec.Body.String())
	}
}

func TestAuthInternalTokenRejectedWhenEmptyConfigured(t *testing.T) {
	mw := NewAuth(stubVerifier{}, "")
	req := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	req.Header.Set("X-Internal-Token", "")
	req.Header.Set("X-Workspace-ID", "wsp_test")
	rec := httptest.NewRecorder()

	mw.Wrap(nextOK()).ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401; body = %s", rec.Code, rec.Body.String())
	}
}

func TestAuthInternalTokenRejectedWhenHeaderEmpty(t *testing.T) {
	mw := NewAuth(stubVerifier{}, "dev-internal-secret")
	req := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	req.Header.Set("X-Workspace-ID", "wsp_test")
	rec := httptest.NewRecorder()

	mw.Wrap(nextOK()).ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401; body = %s", rec.Code, rec.Body.String())
	}
}

func TestAuthInternalTokenIsCaseSensitive(t *testing.T) {
	mw := NewAuth(stubVerifier{}, "Dev-Internal-Secret")
	req := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	req.Header.Set("X-Internal-Token", "dev-internal-secret")
	req.Header.Set("X-Workspace-ID", "wsp_test")
	rec := httptest.NewRecorder()

	mw.Wrap(nextOK()).ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401 for case mismatch; body = %s", rec.Code, rec.Body.String())
	}
}

func TestAuthBearerUsesVerifier(t *testing.T) {
	mw := NewAuth(stubVerifier{tok: &token.Token{ID: "tok_1", WorkspaceID: "wsp_bearer"}}, "")
	req := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	req.Header.Set("Authorization", "Bearer sk-live")
	rec := httptest.NewRecorder()

	mw.Wrap(nextOK()).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var body map[string]string
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body["workspace_id"] != "wsp_bearer" || body["token_id"] != "tok_1" {
		t.Fatalf("unexpected body: %#v", body)
	}
}

func TestAuthBearerInvalid(t *testing.T) {
	mw := NewAuth(stubVerifier{err: token.ErrInvalidToken}, "")
	req := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	req.Header.Set("Authorization", "Bearer sk-bad")
	rec := httptest.NewRecorder()

	mw.Wrap(nextOK()).ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401; body = %s", rec.Code, rec.Body.String())
	}
}

func contains(s, substr string) bool {
	return len(s) >= len(substr) && (s == substr || (len(s) > 0 && indexOf(s, substr) >= 0))
}

func indexOf(s, substr string) int {
	for i := 0; i+len(substr) <= len(s); i++ {
		if s[i:i+len(substr)] == substr {
			return i
		}
	}
	return -1
}
