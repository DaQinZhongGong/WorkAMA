// Package middleware - meter.go implements ⑩计量 (metering).
//
// 该中间件封装 metering.Client.Record，把每次请求的 token 用量写入
// gw_request_log 与 NATS metering 事件。与 Python _log_usage 行为一致：
// 失败仅记录日志，不影响已返回给客户端的响应。
package middleware

import (
	"context"
	"log/slog"
	"time"

	"github.com/workama/workama/apps/gateway/internal/metering"
	"github.com/workama/workama/apps/gateway/internal/token"
)

// MeterMiddleware ⑩计量：发布 NATS metering.llm.v1 事件。
type MeterMiddleware struct {
	Meter  *metering.Client
	Logger *slog.Logger
}

// NewMeter constructs a MeterMiddleware.
func NewMeter(meter *metering.Client, logger *slog.Logger) *MeterMiddleware {
	if logger == nil {
		logger = slog.Default()
	}
	return &MeterMiddleware{Meter: meter, Logger: logger}
}

// Record publishes a metering record. 失败仅记录日志，不返回错误。
// 该方法应在响应已发送给客户端之后调用，使用 context.WithoutCancel
// 防止客户端断开导致 meter 事件丢失。
func (m *MeterMiddleware) Record(parent context.Context, value metering.Record) {
	if m == nil || m.Meter == nil {
		return
	}
	ctx := context.WithoutCancel(parent)
	if err := m.Meter.Record(ctx, value); err != nil {
		m.Logger.Error("metering write failed",
			"request_id", value.RequestID,
			"workspace_id", value.WorkspaceID,
			"error", err,
		)
	}
}

// MeterValue is the data needed to publish a metering event.
// 由 chat_completions.go 在转发完成后填充。
type MeterValue struct {
	RequestID        string
	WorkspaceID      string
	TokenID          string
	ChannelID        string
	Model            string
	PromptTokens     int
	CompletionTokens int
	LatencyMS        int64
	StatusCode       int
	ErrorCode        string
}

// ToRecord converts MeterValue to metering.Record.
func (v MeterValue) ToRecord(tok *token.Token) metering.Record {
	tokenID := v.TokenID
	var tokenPtr *string
	if tokenID != "" {
		tokenPtr = &tokenID
	} else if tok != nil && tok.ID != "" {
		id := tok.ID
		tokenPtr = &id
	}
	rec := metering.Record{
		RequestID:        v.RequestID,
		WorkspaceID:      v.WorkspaceID,
		TokenID:          tokenPtr,
		ChannelID:        v.ChannelID,
		Model:            v.Model,
		PromptTokens:     v.PromptTokens,
		CompletionTokens: v.CompletionTokens,
		LatencyMS:        v.LatencyMS,
		StatusCode:       v.StatusCode,
		ErrorCode:        v.ErrorCode,
	}
	if rec.WorkspaceID == "" && tok != nil {
		rec.WorkspaceID = tok.WorkspaceID
	}
	return rec
}

// RecordLatency is a helper that computes latency from the given start time.
func RecordLatency(start time.Time) int64 {
	return time.Since(start).Milliseconds()
}
