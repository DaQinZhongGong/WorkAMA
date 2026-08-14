// Package channel defines the gateway channel domain.
//
// Channel 是从 gw_channel 表读取的上游渠道配置，包含 base_url、api_key、
// 权重、模型列表与上游模型映射。
package channel

import (
	"context"
	"time"

	"github.com/workama/workama/apps/gateway/internal/relay/adapter"
)

// Channel is the domain model for gw_channel.
type Channel struct {
	ID            string
	WorkspaceID   string
	Name          string
	Provider      string
	Protocol      string
	BaseURL       string
	APIKey        string
	Weight        int
	Models        []string
	UpstreamModel string
	Status        string
	Priority      int
	CreatedAt     time.Time
	UpdatedAt     time.Time
}

// Repository reads channel data from gw_channel.
type Repository interface {
	ListByModel(ctx context.Context, workspaceID, model string) ([]Channel, error)
	GetByID(ctx context.Context, id string) (*Channel, error)
}

// IsEnabled returns true if the channel is in enabled status.
func (c *Channel) IsEnabled() bool {
	if c == nil {
		return false
	}
	return c.Status == "" || c.Status == "enabled"
}

// HasModel returns true if the channel supports the given model.
// 空 Models 视为支持任意模型（与 Python 行为一致）。
func (c *Channel) HasModel(model string) bool {
	if c == nil {
		return false
	}
	if len(c.Models) == 0 {
		return true
	}
	for _, m := range c.Models {
		if m == model {
			return true
		}
	}
	return false
}

// ToAdapter converts a domain Channel to an adapter.Channel.
func (c *Channel) ToAdapter() adapter.Channel {
	if c == nil {
		return adapter.Channel{}
	}
	return adapter.Channel{
		ID:            c.ID,
		WorkspaceID:   c.WorkspaceID,
		Provider:      c.Provider,
		Protocol:      c.Protocol,
		BaseURL:       c.BaseURL,
		APIKey:        c.APIKey,
		Weight:        c.Weight,
		Models:        c.Models,
		UpstreamModel: c.UpstreamModel,
		Status:        c.Status,
	}
}
