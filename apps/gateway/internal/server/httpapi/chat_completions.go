// Package httpapi - chat_completions.go implements POST /v1/chat/completions
// with the full 10-step middleware pipeline:
//
//	① 认证 → ② 授权 → ③ 限流 → ④ 预算 → ⑤ 输入审查 → ⑥ 模型映射
//	→ ⑦ 路由 → ⑧ 转发 → ⑨ 输出审查 → ⑩ 计量
//
// AuthMiddleware 负责 ①认证；Authorize 负责 ②授权；RateLimitMiddleware
// 负责 ③限流；BudgetMiddleware 负责 ④预算。⑤输入审查与 ⑨输出审查
// 当前为简化版（占位），后续可挂载 moderation 服务。本文件实现
// ⑥模型映射 → ⑦路由 → ⑧转发 → ⑩计量。
package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/workama/workama/apps/gateway/internal/channel"
	"github.com/workama/workama/apps/gateway/internal/relay/adapter"
	"github.com/workama/workama/apps/gateway/internal/relay/routing"
	"github.com/workama/workama/apps/gateway/internal/relay/stream"
	"github.com/workama/workama/apps/gateway/internal/server/middleware"
	"github.com/workama/workama/apps/gateway/internal/token"
)

// ChannelRepository reads channel data for routing.
type ChannelRepository interface {
	ListByModel(ctx context.Context, workspaceID, model string) ([]channel.Channel, error)
}

// RequestLogWriter writes the metering row to gw_request_log.
type RequestLogWriter interface {
	InsertRequestLog(ctx context.Context, log *RequestLogRow) error
}

// RequestLogRow is the minimal data written to gw_request_log.
type RequestLogRow struct {
	RequestID        string
	WorkspaceID      string
	TokenID          *string
	ChannelID        string
	Model            string
	PromptTokens     int
	CompletionTokens int
	LatencyMS        int64
	StatusCode       int
	ErrorCode        *string
}

// ModerationService is the optional ⑤/⑨审查服务.
type ModerationService interface {
	// ModerateInput returns false when the input is blocked.
	ModerateInput(ctx context.Context, workspaceID, text, requestID string) (bool, error)
	// ModerateOutput returns false when the output is blocked.
	ModerateOutput(ctx context.Context, workspaceID, text, requestID string) (bool, error)
}

// ChatHandler implements POST /v1/chat/completions.
type ChatHandler struct {
	Channels  ChannelRepository
	LogWriter RequestLogWriter
	Breaker   *routing.CircuitBreaker
	Router    routing.Router
	Meter     *middleware.MeterMiddleware
	Logger    *slog.Logger
	HTTP      *http.Client
}

// NewChat constructs a ChatHandler with the given dependencies.
func NewChat(
	channels ChannelRepository,
	logWriter RequestLogWriter,
	breaker *routing.CircuitBreaker,
	router routing.Router,
	meter *middleware.MeterMiddleware,
	logger *slog.Logger,
) *ChatHandler {
	if logger == nil {
		logger = slog.Default()
	}
	if breaker == nil {
		breaker = routing.NewCircuitBreaker()
	}
	if router == nil {
		router = routing.NewWeightedRouter(nil)
	}
	return &ChatHandler{
		Channels:  channels,
		LogWriter: logWriter,
		Breaker:   breaker,
		Router:    router,
		Meter:     meter,
		Logger:    logger,
		HTTP:      &http.Client{Timeout: 2 * time.Minute},
	}
}

// ChatRequest is the OpenAI-compatible chat completion request body.
type ChatRequest struct {
	Model       string         `json:"model"`
	Messages    []ChatMessage  `json:"messages"`
	Stream      bool           `json:"stream"`
	Temperature *float64       `json:"temperature,omitempty"`
	TopP        *float64       `json:"top_p,omitempty"`
	MaxTokens   *int           `json:"max_tokens,omitempty"`
	Tools       []any          `json:"tools,omitempty"`
	ToolChoice  any            `json:"tool_choice,omitempty"`
	User        string         `json:"user,omitempty"`
	Extra       map[string]any `json:"-"`
}

// ChatMessage is the OpenAI chat message structure.
type ChatMessage struct {
	Role       string `json:"role"`
	Content    any    `json:"content"`
	ToolCallID string `json:"tool_call_id,omitempty"`
	Name       string `json:"name,omitempty"`
	ToolCalls  any    `json:"tool_calls,omitempty"`
}

// ServeHTTP implements http.Handler for POST /v1/chat/completions.
func (h *ChatHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	started := time.Now()
	requestID := requestID(r)
	tok := middleware.TokenFromContext(r.Context())

	// 解析请求体
	var body ChatRequest
	if err := decodeJSON(r.Body, &body); err != nil {
		h.fail(w, r, requestID, tok, started, http.StatusBadRequest, CodeBadRequest, "Invalid JSON request")
		return
	}
	if body.Model == "" || len(body.Messages) == 0 {
		h.fail(w, r, requestID, tok, started, http.StatusBadRequest, CodeBadRequest, "model and messages are required")
		return
	}

	// ②授权：检查模型白名单
	if tok == nil {
		h.fail(w, r, requestID, tok, started, http.StatusUnauthorized, CodeUnauthorized, "Authentication required")
		return
	}
	if !middleware.Authorize(tok, body.Model) {
		h.fail(w, r, requestID, tok, started, http.StatusForbidden, CodeForbidden, "Model is not allowed for this key")
		return
	}

	// ⑤输入审查：占位实现（当前直接放行）
	// 完整管道将调用 ModerationService.ModerateInput()。

	// ⑥模型映射 + ⑦路由：查 gw_channel + 选渠道
	channels, err := h.Channels.ListByModel(r.Context(), tok.WorkspaceID, body.Model)
	if err != nil {
		h.Logger.Error("list channels failed", "request_id", requestID, "error", err)
		h.fail(w, r, requestID, tok, started, http.StatusServiceUnavailable, CodeGatewayError, "Failed to list channels")
		return
	}
	if len(channels) == 0 {
		h.fail(w, r, requestID, tok, started, http.StatusNotFound, CodeNoChannel, "No channel is available for this model")
		return
	}
	// 过滤被熔断的渠道
	candidates := h.filterOpen(channels)
	if len(candidates) == 0 {
		h.fail(w, r, requestID, tok, started, http.StatusServiceUnavailable, CodeGatewayError, "All channels are circuit-broken")
		return
	}
	rt := toRoutingToken(tok)
	// ⑦路由：选出主渠道
	primary, err := h.Router.SelectChannel(r.Context(), candidates, rt)
	if err != nil {
		h.fail(w, r, requestID, tok, started, http.StatusServiceUnavailable, CodeGatewayError, "Routing failed: "+err.Error())
		return
	}

	// ⑧ mock provider：直接返回确定性响应，不发起上游调用（v7.265）。
	// 与 server 包旧路径保持同一 mock 语义；计量/日志走同一管线。
	if primary.Provider == "mock" {
		promptTokens := estimateTokens(messagesText(&body))
		if toolCall := mockToolRequest(&body); toolCall != nil {
			if body.Stream {
				h.streamMockToolCall(w, requestID, body.Model, toolCall)
			} else {
				writeJSON(w, http.StatusOK, map[string]any{
					"id": requestID, "object": "chat.completion", "created": time.Now().Unix(), "model": body.Model,
					"choices": []map[string]any{{"index": 0, "message": map[string]any{"role": "assistant", "content": nil, "tool_calls": []any{toolCall}}, "finish_reason": "tool_calls"}},
					"usage":   map[string]int{"prompt_tokens": promptTokens, "completion_tokens": 1, "total_tokens": promptTokens + 1},
				})
			}
			h.Breaker.Record(primary.ID, false)
			h.recordMeter(r.Context(), requestID, tok, primary, body.Model, &adapter.Usage{PromptTokens: promptTokens, CompletionTokens: 1, TotalTokens: promptTokens + 1}, time.Since(started).Milliseconds(), http.StatusOK, "")
			return
		}
		reply := mockReply(body.Messages)
		completionTokens := estimateTokens(reply)
		if body.Stream {
			h.streamMock(w, r, requestID, body.Model, reply)
		} else {
			writeJSON(w, http.StatusOK, map[string]any{
				"id": requestID, "object": "chat.completion", "created": time.Now().Unix(), "model": body.Model,
				"choices": []map[string]any{{"index": 0, "message": map[string]string{"role": "assistant", "content": reply}, "finish_reason": "stop"}},
				"usage":   map[string]int{"prompt_tokens": promptTokens, "completion_tokens": completionTokens, "total_tokens": promptTokens + completionTokens},
			})
		}
		h.Breaker.Record(primary.ID, false)
		h.recordMeter(r.Context(), requestID, tok, primary, body.Model, &adapter.Usage{PromptTokens: promptTokens, CompletionTokens: completionTokens, TotalTokens: promptTokens + completionTokens}, time.Since(started).Milliseconds(), http.StatusOK, "")
		return
	}

	// ⑧转发：构造 UnifiedRequest 并调用适配器
	adp, err := adapter.ResolveAdapter(primary.Provider)
	if err != nil {
		h.fail(w, r, requestID, tok, started, http.StatusBadGateway, CodeGatewayError, "Unsupported provider: "+primary.Provider)
		return
	}
	unified := toUnifiedRequest(&body)
	upstreamReq, err := adp.BuildRequest(r.Context(), unified, primary)
	if err != nil {
		h.fail(w, r, requestID, tok, started, http.StatusBadGateway, CodeUpstreamError, "Failed to build upstream request")
		return
	}

	// 执行上游请求
	resp, err := h.HTTP.Do(upstreamReq)
	if err != nil {
		h.Breaker.Record(primary.ID, true)
		h.fail(w, r, requestID, tok, started, http.StatusBadGateway, CodeUpstreamError, "Upstream connection error: "+err.Error())
		return
	}
	defer resp.Body.Close()

	if shouldFailover(resp.StatusCode) {
		h.Breaker.Record(primary.ID, true)
		h.fail(w, r, requestID, tok, started, resp.StatusCode, CodeUpstreamStatus, fmt.Sprintf("Upstream returned HTTP %d", resp.StatusCode))
		return
	}

	// ⑧转发 + ⑩计量：根据 stream 选择透传或一次性解析
	if body.Stream {
		h.streamResponse(w, r, resp, adp, primary, requestID, tok, body.Model, started)
		return
	}
	h.nonStreamResponse(w, r, resp, adp, primary, requestID, tok, body.Model, started)
}

// nonStreamResponse handles a non-streaming upstream response.
// 调用适配器 ParseResponse 将上游响应转换为 OpenAI 兼容格式，写入客户端。
func (h *ChatHandler) nonStreamResponse(w http.ResponseWriter, r *http.Request, resp *http.Response, adp adapter.Adapter, primary *adapter.Channel, requestID string, tok *token.Token, model string, started time.Time) {
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		h.Breaker.Record(primary.ID, true)
		h.fail(w, r, requestID, tok, started, http.StatusBadGateway, CodeUpstreamError, "Failed to read upstream response")
		return
	}
	// 解析响应（统一为 OpenAI 格式）
	unified, err := adp.ParseResponse(raw)
	if err != nil {
		h.Breaker.Record(primary.ID, true)
		h.fail(w, r, requestID, tok, started, http.StatusBadGateway, CodeUpstreamStatus, "Failed to parse upstream response")
		return
	}
	if unified.Model == "" {
		unified.Model = model
	}
	h.Breaker.Record(primary.ID, false)

	// ⑨输出审查：占位（完整管道将调用 ModerationService.ModerateOutput()）

	// 写入客户端
	payload, err := json.Marshal(unified)
	if err != nil {
		h.Logger.Error("marshal response failed", "request_id", requestID, "error", err)
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("X-Wama-Channel", primary.ID)
	w.Header().Set("X-Wama-Routing", routingMode(primary))
	w.WriteHeader(resp.StatusCode)
	if payload != nil {
		_, _ = w.Write(payload)
	}

	// ⑩计量：发布 NATS metering 事件 + 写入 gw_request_log
	usage := adp.ExtractUsage(unified)
	latency := time.Since(started).Milliseconds()
	h.recordMeter(r.Context(), requestID, tok, primary, model, usage, latency, resp.StatusCode, "")
	h.writeLog(r.Context(), requestID, tok, primary, model, usage, latency, resp.StatusCode, nil)
}

// streamResponse handles a streaming upstream response.
// stream=true 时调用 stream.Passthrough 透传 SSE，并聚合 token 用量。
func (h *ChatHandler) streamResponse(w http.ResponseWriter, r *http.Request, resp *http.Response, adp adapter.Adapter, primary *adapter.Channel, requestID string, tok *token.Token, model string, started time.Time) {
	flusher, _ := w.(http.Flusher)
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Wama-Channel", primary.ID)
	w.Header().Set("X-Wama-Routing", routingMode(primary))
	w.WriteHeader(http.StatusOK)
	if flusher != nil {
		flusher.Flush()
	}
	usage := &stream.AggregatedUsage{}
	passthroughErr := stream.Passthrough(r.Context(), resp.Body, w, usage, stream.PassthroughOptions{
		Adapter:          adp,
		Model:            model,
		FirstByteTimeout: 15 * time.Second,
	})
	if passthroughErr != nil && !errors.Is(passthroughErr, io.EOF) {
		h.Logger.Warn("sse passthrough ended", "request_id", requestID, "error", passthroughErr)
		h.Breaker.Record(primary.ID, true)
	} else {
		h.Breaker.Record(primary.ID, false)
	}
	// ⑩计量
	aggregated := usage.Aggregate()
	latency := time.Since(started).Milliseconds()
	status := http.StatusOK
	if passthroughErr != nil && !errors.Is(passthroughErr, io.EOF) {
		status = http.StatusBadGateway
	}
	h.recordMeter(r.Context(), requestID, tok, primary, model, aggregated, latency, status, "")
	h.writeLog(r.Context(), requestID, tok, primary, model, aggregated, latency, status, nil)
}

// fail is the shared failure path: write OpenAI error + record meter + log.
func (h *ChatHandler) fail(w http.ResponseWriter, r *http.Request, requestID string, tok *token.Token, started time.Time, status int, code ErrorCode, message string) {
	latency := time.Since(started).Milliseconds()
	WriteErrorWithStatus(w, code, message, status)
	// ⑩计量：失败也记录
	if tok != nil {
		errCode := string(code)
		h.recordMeter(r.Context(), requestID, tok, nil, "", &adapter.Usage{}, latency, status, errCode)
		h.writeLog(r.Context(), requestID, tok, nil, "", &adapter.Usage{}, latency, status, &errCode)
	}
}

// recordMeter publishes a NATS metering.llm.v1 event via the meter middleware.
func (h *ChatHandler) recordMeter(ctx context.Context, requestID string, tok *token.Token, ch *adapter.Channel, model string, usage *adapter.Usage, latency int64, status int, errCode string) {
	if h.Meter == nil {
		return
	}
	value := middleware.MeterValue{
		RequestID:        requestID,
		Model:            model,
		PromptTokens:     usage.PromptTokens,
		CompletionTokens: usage.CompletionTokens,
		LatencyMS:        latency,
		StatusCode:        status,
		ErrorCode:        errCode,
	}
	if tok != nil {
		value.WorkspaceID = tok.WorkspaceID
		value.TokenID = tok.ID
	}
	if ch != nil {
		value.ChannelID = ch.ID
	}
	h.Meter.Record(ctx, value.ToRecord(tok))
}

// writeLog inserts the request log row into gw_request_log.
func (h *ChatHandler) writeLog(ctx context.Context, requestID string, tok *token.Token, ch *adapter.Channel, model string, usage *adapter.Usage, latency int64, status int, errCode *string) {
	if h.LogWriter == nil {
		return
	}
	row := &RequestLogRow{
		RequestID:        requestID,
		WorkspaceID:      tok.WorkspaceID,
		TokenID:          nil,
		ChannelID:         "",
		Model:            model,
		PromptTokens:      usage.PromptTokens,
		CompletionTokens: usage.CompletionTokens,
		LatencyMS:         latency,
		StatusCode:        status,
		ErrorCode:         errCode,
	}
	if tok != nil && tok.ID != "" {
		id := tok.ID
		row.TokenID = &id
	}
	if ch != nil {
		row.ChannelID = ch.ID
	}
	// 写入失败不影响响应，仅在日志中记录
	if err := h.LogWriter.InsertRequestLog(ctx, row); err != nil {
		h.Logger.Warn("insert gw_request_log failed", "request_id", requestID, "error", err)
	}
}

// filterOpen returns channels that the breaker currently allows.
func (h *ChatHandler) filterOpen(channels []channel.Channel) []adapter.Channel {
	out := make([]adapter.Channel, 0, len(channels))
	for _, c := range channels {
		if !h.Breaker.Allow(c.ID) {
			continue
		}
		out = append(out, c.ToAdapter())
	}
	return out
}

// toUnifiedRequest converts the chat request body to adapter.UnifiedRequest.
func toUnifiedRequest(body *ChatRequest) *adapter.UnifiedRequest {
	messages := make([]adapter.Message, 0, len(body.Messages))
	for _, m := range body.Messages {
		messages = append(messages, adapter.Message{
			Role:       m.Role,
			Content:    m.Content,
			ToolCallID: m.ToolCallID,
			Name:       m.Name,
			ToolCalls:  m.ToolCalls,
		})
	}
	return &adapter.UnifiedRequest{
		Model:       body.Model,
		Messages:    messages,
		Stream:      body.Stream,
		Temperature: body.Temperature,
		TopP:        body.TopP,
		MaxTokens:   body.MaxTokens,
		Tools:       body.Tools,
		ToolChoice:  body.ToolChoice,
		Extra:       body.Extra,
	}
}

// toRoutingToken converts a domain token to a routing.Token.
func toRoutingToken(tok *token.Token) *routing.Token {
	if tok == nil {
		return nil
	}
	return &routing.Token{
		ID:             tok.ID,
		WorkspaceID:    tok.WorkspaceID,
		GroupID:        tok.GroupID,
		PinnedChannel:  tok.PinnedChannelID,
		ModelWhitelist: tok.ModelWhitelist,
	}
}

// routingMode returns the routing mode label for the X-Wama-Routing header.
// 当 token 的 pinned_channel_id 与选中渠道一致时视为 pinned。
func routingMode(ch *adapter.Channel) string {
	if ch == nil {
		return "weighted"
	}
	if ch.PinnedChannel != "" && ch.PinnedChannel == ch.ID {
		return "pinned"
	}
	return "weighted"
}

// shouldFailover returns true when the status code indicates a failover.
func shouldFailover(statusCode int) bool {
	return statusCode == http.StatusUnauthorized ||
		statusCode == http.StatusForbidden ||
		statusCode == http.StatusRequestTimeout ||
		statusCode == http.StatusTooManyRequests ||
		statusCode >= http.StatusInternalServerError
}

// requestID extracts the request ID from headers or context, generating a
// fallback when missing. 与 server.go 的 requestID() 行为一致。
func requestID(r *http.Request) string {
	if v := r.Header.Get("X-Wama-Request-ID"); v != "" {
		return v
	}
	if v := r.Header.Get("X-Request-ID"); v != "" {
		return v
	}
	return fmt.Sprintf("req_%d", time.Now().UnixNano())
}

// decodeJSON mirrors server.go's decodeJSON helper.
func decodeJSON(reader io.Reader, target any) error {
	decoder := json.NewDecoder(io.LimitReader(reader, 4<<20))
	return decoder.Decode(target)
}

// estimateTokens mirrors server.go's estimateTokens helper.
// 粗略估算：UTF-8 字符数 / 4。
// messagesText 拼接消息文本（用于 mock 路径的 prompt tokens 估算）。
func messagesText(body *ChatRequest) string {
	var parts []string
	for _, message := range body.Messages {
		if content, ok := message.Content.(string); ok {
			parts = append(parts, content)
		}
	}
	return strings.Join(parts, " ")
}

func estimateTokens(value string) int {
	count := utf8.RuneCountInString(value) / 4
	if count < 1 {
		return 1
	}
	return count
}

// estimateMessageTokens sums estimateTokens over all string messages.
func estimateMessageTokens(messages []ChatMessage) int {
	total := 0
	for _, m := range messages {
		if s, ok := m.Content.(string); ok {
			total += estimateTokens(s) + 4
		}
	}
	if total == 0 {
		return 1
	}
	return total
}

// trim is a small helper retained for future handlers.
var _ = strings.TrimSpace

// Compile-time assertion that ChatHandler implements http.Handler.
var _ http.Handler = (*ChatHandler)(nil)
