// Package middleware implements the 10-step pipeline middleware.
//
// 步骤顺序：①认证 → ②授权 → ③限流 → ④预算 → ⑤输入审查 → ⑥模型映射
// → ⑦路由 → ⑧转发 → ⑨输出审查 → ⑩计量。
package middleware

import (
	"context"
	"errors"
	"net/http"
	"strings"

	"github.com/workama/workama/apps/gateway/internal/server/httperr"
	"github.com/workama/workama/apps/gateway/internal/token"
)

// contextKey is the typed key for context values.
type contextKey string

const (
	// TokenContextKey holds the authenticated *token.Token in request context.
	TokenContextKey contextKey = "gateway.token"
	// APIKeyContextKey holds the raw bearer API key (used by routing decisions).
	APIKeyContextKey contextKey = "gateway.api_key"
)

// AuthMiddleware ①认证：解析 Bearer token，调用 token.Verifier 校验。
// 校验成功后将 *token.Token 注入 context。
// 也支持内部调用方使用 X-Internal-Token + X-Workspace-ID（与旧 server 路径一致）。
type AuthMiddleware struct {
	Verifier      token.Verifier
	InternalToken string
}

// NewAuth constructs an AuthMiddleware.
func NewAuth(verifier token.Verifier, internalToken string) *AuthMiddleware {
	return &AuthMiddleware{Verifier: verifier, InternalToken: internalToken}
}

// ParseBearer extracts the Bearer token from the Authorization header.
// Returns "" when the header is missing or malformed.
func ParseBearer(r *http.Request) string {
	auth := r.Header.Get("Authorization")
	if auth == "" {
		auth = r.Header.Get("authorization")
	}
	if auth == "" {
		return ""
	}
	if !strings.HasPrefix(auth, "Bearer ") && !strings.HasPrefix(auth, "bearer ") {
		return ""
	}
	return strings.TrimSpace(auth[7:])
}

// Wrap wraps the next handler with bearer token verification.
// 失败时返回 OpenAI 兼容 E01001 错误。
func (m *AuthMiddleware) Wrap(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		apiKey := ParseBearer(r)
		if apiKey == "" {
			// v7.265: 内部调用方（agent-server 等）用 X-Internal-Token + X-Workspace-ID。
			internal := r.Header.Get("X-Internal-Token")
			if internal == "" || m.InternalToken == "" || !strings.EqualFold(internal, m.InternalToken) {
				httperr.Write(w, httperr.CodeUnauthorized, "Missing Authorization Bearer token")
				return
			}
			workspaceID := r.Header.Get("X-Workspace-ID")
			if workspaceID == "" {
				httperr.Write(w, httperr.CodeUnauthorized, "Missing X-Workspace-ID for internal token")
				return
			}
			internalTok := &token.Token{
				ID:             "internal",
				WorkspaceID:    workspaceID,
				Name:           "internal",
				KeyHash:        "internal",
				KeyPepper:      "",
				ModelWhitelist: nil, // 内部调用不限制模型
			}
			ctx := context.WithValue(r.Context(), TokenContextKey, internalTok)
			ctx = context.WithValue(ctx, APIKeyContextKey, "")
			next.ServeHTTP(w, r.WithContext(ctx))
			return
		}
		if m.Verifier == nil {
			httperr.Write(w, httperr.CodeGatewayError, "Token verifier not configured")
			return
		}
		tok, err := m.Verifier.Verify(r.Context(), apiKey)
		if err != nil {
			if errors.Is(err, token.ErrExpiredToken) {
				httperr.Write(w, httperr.CodeUnauthorized, "API key expired")
				return
			}
			if errors.Is(err, token.ErrInvalidToken) {
				httperr.Write(w, httperr.CodeUnauthorized, "Invalid API key")
				return
			}
			httperr.Write(w, httperr.CodeGatewayError, "Token verification failed")
			return
		}
		ctx := context.WithValue(r.Context(), TokenContextKey, tok)
		ctx = context.WithValue(ctx, APIKeyContextKey, apiKey)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

// TokenFromContext returns the *token.Token injected by AuthMiddleware.
// Returns nil when no token is in the context.
func TokenFromContext(ctx context.Context) *token.Token {
	if v, ok := ctx.Value(TokenContextKey).(*token.Token); ok {
		return v
	}
	return nil
}

// APIKeyFromContext returns the raw bearer key from context.
func APIKeyFromContext(ctx context.Context) string {
	if v, ok := ctx.Value(APIKeyContextKey).(string); ok {
		return v
	}
	return ""
}

// Authorize ②授权：检查模型白名单。
// 当 token.ModelWhitelist 非空且不包含 model 时返回 false（拒绝）。
func Authorize(tok *token.Token, model string) bool {
	return tok != nil && tok.CanAccessModel(model)
}
