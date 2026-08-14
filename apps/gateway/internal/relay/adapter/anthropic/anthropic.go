// Package anthropic implements the Anthropic Messages API adapter.
package anthropic

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/workama/workama/apps/gateway/internal/relay/adapter"
)

// Adapter implements adapter.Adapter for Anthropic Messages API upstreams.
type Adapter struct{}

// New returns an Anthropic adapter.
func New() *Adapter { return &Adapter{} }

func init() {
	adapter.RegisterFactory("anthropic", func(_ string) (adapter.Adapter, error) {
		return New(), nil
	})
}

// DescribeCapabilities returns the Anthropic capability profile.
func (a *Adapter) DescribeCapabilities(_ context.Context, ch *adapter.Channel) (*adapter.CapabilityProfile, error) {
	provider := ch.Provider
	if provider == "" {
		provider = "anthropic"
	}
	profile := adapter.DescribeCapabilities(provider)
	if profile.NativeProtocol == "" {
		profile.NativeProtocol = "anthropic"
	}
	return profile, nil
}

// BuildRequest builds a POST {base_url}/v1/messages request with x-api-key.
// system 消息抽到顶层，messages 转换为 Anthropic 格式，强制 max_tokens。
func (a *Adapter) BuildRequest(ctx context.Context, unified *adapter.UnifiedRequest, ch *adapter.Channel) (*http.Request, error) {
	if unified == nil {
		return nil, fmt.Errorf("unified request is nil")
	}
	model := ch.UpstreamModel
	if model == "" {
		model = unified.Model
	}
	maxTokens := 1024
	if unified.MaxTokens != nil && *unified.MaxTokens > 0 {
		maxTokens = *unified.MaxTokens
	}
	systemPrompt := ""
	messages := make([]map[string]any, 0, len(unified.Messages))
	for _, msg := range unified.Messages {
		content, _ := msg.Content.(string)
		if content == "" {
			if raw, err := json.Marshal(msg.Content); err == nil {
				content = string(raw)
			}
		}
		if msg.Role == "system" {
			if systemPrompt != "" {
				systemPrompt += "\n"
			}
			systemPrompt += content
			continue
		}
		role := msg.Role
		if role != "user" && role != "assistant" {
			role = "user"
		}
		messages = append(messages, map[string]any{"role": role, "content": content})
	}
	body := map[string]any{
		"model":      model,
		"messages":   messages,
		"max_tokens": maxTokens,
		"stream":     unified.Stream,
	}
	if systemPrompt != "" {
		body["system"] = systemPrompt
	}
	if unified.Temperature != nil {
		body["temperature"] = *unified.Temperature
	}
	if unified.TopP != nil {
		body["top_p"] = *unified.TopP
	}
	if len(unified.Tools) > 0 {
		body["tools"] = unified.Tools
	}
	if unified.ToolChoice != nil {
		body["tool_choice"] = unified.ToolChoice
	}
	for k, v := range unified.Extra {
		if _, exists := body[k]; !exists {
			body[k] = v
		}
	}
	payload, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	base := strings.TrimRight(ch.BaseURL, "/")
	endpoint := base + "/v1/messages"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(payload))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	if ch.APIKey != "" {
		req.Header.Set("x-api-key", ch.APIKey)
	}
	req.Header.Set("anthropic-version", "2023-06-01")
	return req, nil
}

// ParseResponse parses an Anthropic Messages response into UnifiedResponse.
// content[].text 拼接，usage.input/output_tokens → prompt/completion_tokens。
func (a *Adapter) ParseResponse(raw []byte) (*adapter.UnifiedResponse, error) {
	var src map[string]any
	if err := json.Unmarshal(raw, &src); err != nil {
		return nil, fmt.Errorf("decode anthropic response: %w", err)
	}
	text := ""
	if blocks, ok := src["content"].([]any); ok {
		for _, raw := range blocks {
			if block, ok := raw.(map[string]any); ok && block["type"] == "text" {
				if t, _ := block["text"].(string); t != "" {
					text += t
				}
			}
		}
	}
	finishReason := "stop"
	if src["stop_reason"] == "max_tokens" {
		finishReason = "length"
	}
	usage := &adapter.Usage{}
	if u, ok := src["usage"].(map[string]any); ok {
		usage.PromptTokens = toInt(u["input_tokens"])
		usage.CompletionTokens = toInt(u["output_tokens"])
		usage.TotalTokens = usage.PromptTokens + usage.CompletionTokens
	}
	id, _ := src["id"].(string)
	if id == "" {
		id = "chatcmpl-anthropic"
	}
	resp := &adapter.UnifiedResponse{
		ID:      id,
		Object:  "chat.completion",
		Created: time.Now().Unix(),
		Choices: []adapter.Choice{{
			Index:        0,
			Message:      adapter.Message{Role: "assistant", Content: text},
			FinishReason: finishReason,
		}},
		Usage: usage,
		Raw:  src,
	}
	return resp, nil
}

// ParseStreamChunk parses an Anthropic SSE chunk into UnifiedChunk.
// Anthropic SSE 事件类型：message_start / content_block_start / content_block_delta
// / message_delta / message_stop。
func (a *Adapter) ParseStreamChunk(raw []byte) (*adapter.UnifiedChunk, error) {
	text := strings.TrimSpace(string(raw))
	if text == "[DONE]" {
		return &adapter.UnifiedChunk{Done: true}, nil
	}
	var event map[string]any
	if err := json.Unmarshal(raw, &event); err != nil {
		return nil, fmt.Errorf("decode anthropic stream chunk: %w", err)
	}
	chunk := &adapter.UnifiedChunk{
		ID:      "chatcmpl-anthropic",
		Object:  "chat.completion.chunk",
		Created: time.Now().Unix(),
	}
	deltaText := ""
	deltaType, _ := event["type"].(string)
	switch deltaType {
	case "message_start":
		if msg, ok := event["message"].(map[string]any); ok {
			if id, _ := msg["id"].(string); id != "" {
				chunk.ID = id
			}
		}
	case "content_block_delta":
		if delta, ok := event["delta"].(map[string]any); ok {
			if t, _ := delta["text"].(string); t != "" {
				deltaText = t
			}
		}
	case "message_delta":
		if delta, ok := event["delta"].(map[string]any); ok {
			if stop, _ := delta["stop_reason"].(string); stop != "" {
				reason := mapAnthropicStop(stop)
				chunk.Choices = append(chunk.Choices, adapter.ChunkChoice{
					Index:        0,
					Delta:        adapter.Message{Role: "assistant"},
					FinishReason: &reason,
				})
			}
			if u, ok := event["usage"].(map[string]any); ok {
				chunk.Usage = &adapter.Usage{
					CompletionTokens: toInt(u["output_tokens"]),
				}
				chunk.Usage.TotalTokens = chunk.Usage.PromptTokens + chunk.Usage.CompletionTokens
			}
		}
	case "message_stop":
		chunk.Done = true
	}
	if deltaText != "" {
		chunk.Choices = append(chunk.Choices, adapter.ChunkChoice{
			Index: 0,
			Delta: adapter.Message{
				Role:    "assistant",
				Content: deltaText,
			},
		})
	}
	chunk.Raw = event
	return chunk, nil
}

// ExtractUsage returns the Usage from a parsed Anthropic response.
func (a *Adapter) ExtractUsage(resp *adapter.UnifiedResponse) *adapter.Usage {
	if resp == nil || resp.Usage == nil {
		return &adapter.Usage{}
	}
	u := *resp.Usage
	if u.TotalTokens == 0 {
		u.TotalTokens = u.PromptTokens + u.CompletionTokens
	}
	return &u
}

// HealthCheck pings the upstream /v1/messages endpoint with a HEAD request.
func (a *Adapter) HealthCheck(ctx context.Context, ch *adapter.Channel) error {
	if ch == nil || ch.BaseURL == "" {
		return fmt.Errorf("channel base_url is empty")
	}
	base := strings.TrimRight(ch.BaseURL, "/")
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, base+"/v1/models", nil)
	if err != nil {
		return err
	}
	if ch.APIKey != "" {
		req.Header.Set("x-api-key", ch.APIKey)
	}
	req.Header.Set("anthropic-version", "2023-06-01")
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
	if resp.StatusCode >= 400 {
		return fmt.Errorf("upstream health check returned %d", resp.StatusCode)
	}
	return nil
}

// QueryBalance is not implemented for Anthropic.
func (a *Adapter) QueryBalance(_ context.Context, ch *adapter.Channel) (*adapter.Balance, error) {
	return &adapter.Balance{Provider: ch.Provider, Amount: 0, Currency: "USD"}, nil
}

func toInt(value any) int {
	switch v := value.(type) {
	case float64:
		return int(v)
	case int:
		return v
	case int64:
		return int(v)
	case json.Number:
		if n, err := v.Int64(); err == nil {
			return int(n)
		}
	}
	return 0
}

func mapAnthropicStop(reason string) string {
	switch reason {
	case "max_tokens":
		return "length"
	case "tool_use":
		return "tool_calls"
	case "end_turn", "stop_sequence":
		fallthrough
	default:
		return "stop"
	}
}
