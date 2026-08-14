// Package gemini implements the Gemini generateContent adapter.
package gemini

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/workama/workama/apps/gateway/internal/relay/adapter"
)

// Adapter implements adapter.Adapter for Gemini generateContent upstreams.
type Adapter struct{}

// New returns a Gemini adapter.
func New() *Adapter { return &Adapter{} }

func init() {
	adapter.RegisterFactory("gemini", func(_ string) (adapter.Adapter, error) {
		return New(), nil
	})
}

// DescribeCapabilities returns the Gemini capability profile.
func (a *Adapter) DescribeCapabilities(_ context.Context, ch *adapter.Channel) (*adapter.CapabilityProfile, error) {
	provider := ch.Provider
	if provider == "" {
		provider = "gemini"
	}
	profile := adapter.DescribeCapabilities(provider)
	if profile.NativeProtocol == "" {
		profile.NativeProtocol = "gemini"
	}
	return profile, nil
}

// BuildRequest builds a POST {base_url}/models/{model}:generateContent?key={api_key} request.
// messages → contents，role 仅 user/model，generationConfig 透传 max/temperature。
func (a *Adapter) BuildRequest(ctx context.Context, unified *adapter.UnifiedRequest, ch *adapter.Channel) (*http.Request, error) {
	if unified == nil {
		return nil, fmt.Errorf("unified request is nil")
	}
	model := ch.UpstreamModel
	if model == "" {
		model = unified.Model
	}
	contents := make([]map[string]any, 0, len(unified.Messages))
	for _, msg := range unified.Messages {
		role := "user"
		if msg.Role == "assistant" || msg.Role == "model" {
			role = "model"
		}
		text, _ := msg.Content.(string)
		if text == "" {
			if raw, err := json.Marshal(msg.Content); err == nil {
				text = string(raw)
			}
		}
		contents = append(contents, map[string]any{
			"role":  role,
			"parts": []map[string]any{{"text": text}},
		})
	}
	genConfig := map[string]any{}
	if unified.MaxTokens != nil {
		genConfig["maxOutputTokens"] = *unified.MaxTokens
	} else {
		genConfig["maxOutputTokens"] = 1024
	}
	if unified.Temperature != nil {
		genConfig["temperature"] = *unified.Temperature
	}
	if unified.TopP != nil {
		genConfig["topP"] = *unified.TopP
	}
	body := map[string]any{
		"contents":         contents,
		"generationConfig": genConfig,
	}
	if len(unified.Tools) > 0 {
		body["tools"] = unified.Tools
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
	method := "generateContent"
	if unified.Stream {
		method = "streamGenerateContent"
	}
	endpoint := base + "/models/" + url.PathEscape(model) + ":" + method
	if ch.APIKey != "" {
		endpoint += "?key=" + url.QueryEscape(ch.APIKey)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(payload))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	return req, nil
}

// ParseResponse parses a Gemini generateContent response into UnifiedResponse.
// candidates[].content.parts[].text 拼接，finishReason 映射，usageMetadata 抽取。
func (a *Adapter) ParseResponse(raw []byte) (*adapter.UnifiedResponse, error) {
	var src map[string]any
	if err := json.Unmarshal(raw, &src); err != nil {
		return nil, fmt.Errorf("decode gemini response: %w", err)
	}
	text := ""
	finishReason := "stop"
	if candidates, ok := src["candidates"].([]any); ok && len(candidates) > 0 {
		if candidate, ok := candidates[0].(map[string]any); ok {
			if content, ok := candidate["content"].(map[string]any); ok {
				if parts, ok := content["parts"].([]any); ok {
					for _, rawPart := range parts {
						if part, ok := rawPart.(map[string]any); ok {
							if t, _ := part["text"].(string); t != "" {
								text += t
							}
						}
					}
				}
			}
			if reason, _ := candidate["finishReason"].(string); reason != "" {
				finishReason = mapGeminiFinish(reason)
			}
		}
	}
	usage := &adapter.Usage{}
	if meta, ok := src["usageMetadata"].(map[string]any); ok {
		usage.PromptTokens = toInt(meta["promptTokenCount"])
		usage.CompletionTokens = toInt(meta["candidatesTokenCount"])
		usage.TotalTokens = toInt(meta["totalTokenCount"])
		if usage.TotalTokens == 0 {
			usage.TotalTokens = usage.PromptTokens + usage.CompletionTokens
		}
	}
	return &adapter.UnifiedResponse{
		ID:      "chatcmpl-gemini",
		Object:  "chat.completion",
		Created: time.Now().Unix(),
		Choices: []adapter.Choice{{
			Index:        0,
			Message:      adapter.Message{Role: "assistant", Content: text},
			FinishReason: finishReason,
		}},
		Usage: usage,
		Raw:  src,
	}, nil
}

// ParseStreamChunk parses a Gemini SSE chunk into UnifiedChunk.
func (a *Adapter) ParseStreamChunk(raw []byte) (*adapter.UnifiedChunk, error) {
	text := strings.TrimSpace(string(raw))
	if text == "[DONE]" {
		return &adapter.UnifiedChunk{Done: true}, nil
	}
	var src map[string]any
	if err := json.Unmarshal(raw, &src); err != nil {
		return nil, fmt.Errorf("decode gemini stream chunk: %w", err)
	}
	chunk := &adapter.UnifiedChunk{
		ID:      "chatcmpl-gemini",
		Object:  "chat.completion.chunk",
		Created: time.Now().Unix(),
	}
	deltaText := ""
	done := false
	if candidates, ok := src["candidates"].([]any); ok && len(candidates) > 0 {
		if candidate, ok := candidates[0].(map[string]any); ok {
			if content, ok := candidate["content"].(map[string]any); ok {
				if parts, ok := content["parts"].([]any); ok && len(parts) > 0 {
					if part, ok := parts[0].(map[string]any); ok {
						deltaText, _ = part["text"].(string)
					}
				}
			}
			if reason, _ := candidate["finishReason"].(string); reason != "" {
				finishReason := mapGeminiFinish(reason)
				chunk.Choices = append(chunk.Choices, adapter.ChunkChoice{
					Index:        0,
					Delta:        adapter.Message{Role: "assistant"},
					FinishReason: &finishReason,
				})
				done = true
			}
		}
	}
	if meta, ok := src["usageMetadata"].(map[string]any); ok {
		chunk.Usage = &adapter.Usage{
			PromptTokens:     toInt(meta["promptTokenCount"]),
			CompletionTokens: toInt(meta["candidatesTokenCount"]),
		}
		chunk.Usage.TotalTokens = chunk.Usage.PromptTokens + chunk.Usage.CompletionTokens
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
	if done && len(chunk.Choices) == 0 {
		chunk.Done = true
	}
	chunk.Raw = src
	return chunk, nil
}

// ExtractUsage returns the Usage from a parsed Gemini response.
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

// HealthCheck pings the upstream Gemini models endpoint.
func (a *Adapter) HealthCheck(ctx context.Context, ch *adapter.Channel) error {
	if ch == nil || ch.BaseURL == "" {
		return fmt.Errorf("channel base_url is empty")
	}
	base := strings.TrimRight(ch.BaseURL, "/")
	endpoint := base + "/models"
	if ch.APIKey != "" {
		endpoint += "?key=" + url.QueryEscape(ch.APIKey)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return err
	}
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

// QueryBalance is not implemented for Gemini.
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

func mapGeminiFinish(reason string) string {
	switch reason {
	case "STOP":
		return "stop"
	case "MAX_TOKENS":
		return "length"
	case "SAFETY", "RECITATION":
		return "content_filter"
	default:
		return "stop"
	}
}
