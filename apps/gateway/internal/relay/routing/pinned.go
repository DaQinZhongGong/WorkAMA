package routing

import (
	"context"

	"github.com/workama/workama/apps/gateway/internal/relay/adapter"
)

// PinnedRouter returns the channel pinned to the token group.
// 与 Python 端 pinned_channel_id 行为一致：直接返回 pinned_channel_id 对应的渠道。
type PinnedRouter struct{}

// NewPinnedRouter creates a PinnedRouter.
func NewPinnedRouter() *PinnedRouter { return &PinnedRouter{} }

// SelectChannel implements Router.
// 当 token.PinnedChannel 为空时退化为返回第一个候选（与 Python 一致）。
func (r *PinnedRouter) SelectChannel(_ context.Context, candidates []adapter.Channel, token *Token) (*adapter.Channel, error) {
	if len(candidates) == 0 {
		return nil, ErrNoChannels
	}
	if token == nil || token.PinnedChannel == "" {
		first := candidates[0]
		return &first, nil
	}
	for _, ch := range candidates {
		if ch.ID == token.PinnedChannel {
			return &ch, nil
		}
	}
	return nil, ErrPinnedUnavailable
}

// assertPinnedRouter is a compile-time check.
var _ Router = (*PinnedRouter)(nil)
