// Package routing defines the routing strategy interface used by the
// 10-step pipeline to select an upstream channel.
package routing

import (
	"context"
	"errors"

	"github.com/workama/workama/apps/gateway/internal/relay/adapter"
)

// Token is a minimal view of the authenticated token required for routing.
// The full token domain lives in apps/gateway/internal/token.
type Token struct {
	ID            string
	WorkspaceID   string
	GroupID       string
	PinnedChannel string
	ModelWhitelist []string
}

// Router selects a channel from a candidate list given a token context.
type Router interface {
	SelectChannel(ctx context.Context, candidates []adapter.Channel, token *Token) (*adapter.Channel, error)
}

// ErrNoChannels is returned when the candidate list is empty.
var ErrNoChannels = errors.New("no candidate channels available for routing")

// ErrPinnedUnavailable is returned when the pinned channel is missing from candidates.
var ErrPinnedUnavailable = errors.New("pinned channel is not in the candidate list")
