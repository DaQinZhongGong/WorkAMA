// Package httpapi - models.go implements GET /v1/models.
//
// 聚合工作空间下所有 enabled 渠道的 models 数组，按令牌白名单过滤。
// 与 Python relay.list_models 行为一致。
package httpapi

import (
	"context"
	"net/http"
	"time"

	"github.com/workama/workama/apps/gateway/internal/channel"
	"github.com/workama/workama/apps/gateway/internal/server/middleware"
	"github.com/workama/workama/apps/gateway/internal/token"
)

// ModelsHandler handles GET /v1/models.
type ModelsHandler struct {
	Channels ChannelLister
}

// ChannelLister returns all enabled channels for a workspace.
// 由 store/pg.Gateway 实现（ListChannelsByWorkspace）。
type ChannelLister interface {
	ListChannelsByWorkspace(ctx context.Context, workspaceID string) ([]channel.Channel, error)
}

// ModelsResponse is the OpenAI-compatible /v1/models response.
type ModelsResponse struct {
	Object string      `json:"object"`
	Data   []ModelEntry `json:"data"`
}

// ModelEntry is a single model entry in the list response.
type ModelEntry struct {
	ID      string `json:"id"`
	Object  string `json:"object"`
	Created int64  `json:"created"`
	OwnedBy string `json:"owned_by"`
}

// ServeHTTP implements http.Handler.
// 复用 AuthMiddleware 注入的 *token.Token 来过滤模型白名单。
func (h *ModelsHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	tok := middleware.TokenFromContext(r.Context())
	if tok == nil {
		WriteError(w, CodeUnauthorized, "Authentication required")
		return
	}
	if h.Channels == nil {
		WriteError(w, CodeGatewayError, "Channel store not configured")
		return
	}
	channels, err := h.Channels.ListChannelsByWorkspace(r.Context(), tok.WorkspaceID)
	if err != nil {
		WriteError(w, CodeGatewayError, "Failed to list channels")
		return
	}
	seen := map[string]bool{}
	entries := []ModelEntry{}
	now := time.Now().Unix()
	for _, ch := range channels {
		for _, model := range ch.Models {
			if model == "" || seen[model] {
				continue
			}
			if !filterModel(tok, model) {
				continue
			}
			seen[model] = true
			entries = append(entries, ModelEntry{
				ID:      model,
				Object:  "model",
				Created: now,
				OwnedBy: ch.Provider,
			})
		}
	}
	writeJSON(w, http.StatusOK, ModelsResponse{
		Object: "list",
		Data:   entries,
	})
}

// filterModel returns true if the token's whitelist permits the model.
// Empty whitelist allows all models.
func filterModel(tok *token.Token, model string) bool {
	if tok == nil {
		return false
	}
	return tok.CanAccessModel(model)
}
