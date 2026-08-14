// Package middleware - budget.go implements ④预算 (budget check).
//
// 简化版预算检查：直接查询 bill_account 表的余额（>=0 视为可用）。
// 完整管道还会在转发前 Reservation、在 meter 后 Settlement，
// 由 platform-api 负责。本中间件只做最小可用性检查。
package middleware

import (
	"context"
	"errors"
	"net/http"

	"github.com/workama/workama/apps/gateway/internal/server/httperr"
	"github.com/workama/workama/apps/gateway/internal/token"
)

// BalanceService is the minimal budget service interface.
type BalanceService interface {
	// HasCredit returns true when the workspace has at least 0 credits
	// remaining. 简化版实现仅检查余额是否 >= 0。
	HasCredit(ctx context.Context, workspaceID string) (bool, error)
}

// BudgetMiddleware ④预算：检查工作空间余额是否可用。
type BudgetMiddleware struct {
	Balance BalanceService
}

// NewBudget constructs a BudgetMiddleware.
func NewBudget(balance BalanceService) *BudgetMiddleware {
	return &BudgetMiddleware{Balance: balance}
}

// Check verifies that the token's workspace has a non-negative balance.
// 失败时返回 OpenAI 兼容 E01004 错误。
func (m *BudgetMiddleware) Check(ctx context.Context, tok *token.Token) bool {
	if m == nil || m.Balance == nil {
		return true
	}
	if tok == nil || tok.WorkspaceID == "" {
		return false
	}
	ok, err := m.Balance.HasCredit(ctx, tok.WorkspaceID)
	if err != nil {
		// 容错：预算服务不可用时放行，避免单点故障阻断所有请求。
		// 失败仍会由 meter 阶段记录到 gw_request_log。
		return true
	}
	return ok
}

// Wrap applies budget verification.
func (m *BudgetMiddleware) Wrap(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		tok := TokenFromContext(r.Context())
		if !m.Check(r.Context(), tok) {
			httperr.Write(w, httperr.CodeInsufficientBalance, "Credit balance is insufficient")
			return
		}
		next.ServeHTTP(w, r)
	})
}

// ErrBudgetServiceUnavailable is returned when the budget service cannot be reached.
var ErrBudgetServiceUnavailable = errors.New("budget service unavailable")
