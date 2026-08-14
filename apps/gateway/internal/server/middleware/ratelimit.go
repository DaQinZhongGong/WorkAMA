// Package middleware - ratelimit.go implements ③限流 (rate limiting).
//
// 该中间件复用 server.Limiter 的窗口计数逻辑，按令牌或工作空间进行限流。
package middleware

import (
	"net/http"
	"strconv"
	"time"

	"github.com/workama/workama/apps/gateway/internal/server"
	"github.com/workama/workama/apps/gateway/internal/server/httperr"
	"github.com/workama/workama/apps/gateway/internal/token"
)

// RateLimitMiddleware ③限流：基于令牌/工作空间的 RPM 限流。
// TPM 限流需要估算 token 数量，由调用方在调用 AllowWithTokens 时传入。
type RateLimitMiddleware struct {
	Limiter *server.Limiter
}

// NewRateLimit constructs a RateLimitMiddleware backed by server.Limiter.
func NewRateLimit(limiter *server.Limiter) *RateLimitMiddleware {
	return &RateLimitMiddleware{Limiter: limiter}
}

// Allow checks whether the actor identified by token is allowed a single RPM.
// 当 tok.RPMLimit <= 0 时视为无限制（与 Python 行为一致：0 表示不限制）。
func (m *RateLimitMiddleware) Allow(tok *token.Token) bool {
	if m == nil || m.Limiter == nil {
		return true
	}
	if tok == nil || tok.RPMLimit <= 0 {
		return true
	}
	key := actorKey(tok)
	return m.Limiter.Allow(key, tok.RPMLimit)
}

// Wrap applies rate limiting based on the token in context.
// 该中间件需要 AuthMiddleware 先把 *token.Token 注入 context。
func (m *RateLimitMiddleware) Wrap(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		tok := TokenFromContext(r.Context())
		if !m.Allow(tok) {
			w.Header().Set("Retry-After", strconv.Itoa(int(time.Minute/time.Second)))
			httperr.Write(w, httperr.CodeRateLimited, "Rate limit exceeded")
			return
		}
		next.ServeHTTP(w, r)
	})
}

// actorKey returns the limiter key for a token. Falls back to workspace
// when the token ID is unavailable.
func actorKey(tok *token.Token) string {
	if tok.ID != "" {
		return "token:" + tok.ID
	}
	return "workspace:" + tok.WorkspaceID
}
