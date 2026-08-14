// Package openai implements the OpenAI-compatible adapter.
package openai

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

// Adapter implements adapter.Adapter for OpenAI-compatible upstreams.
type Adapter struct{}

// New returns an OpenAI adapter.
func New() *Adapter { return &Adapter{} }

func init() {
	adapter.RegisterFactory("openai", func(_ string) (adapter.Adapter, error) {
		return New(), nil
	})
}

// DescribeCapabilities returns the OpenAI capability profile.
func (a *Adapter) DescribeCapabilities(_ context.Context, ch *adapter.Channel) (*adapter.CapabilityProfile, error) {
	provider := ch.Provider
	if provider == "" {
		provider = "openai"
	}
	profile := adapter.DescribeCapabilities(provider)
	if profile.NativeProtocol == "" {
		profile.NativeProtocol = "openai"
	}
	return profile, nil
}

// BuildRequest builds a POST {base_url}/chat/completions request.
func (a *Adapter) BuildRequest(ctx context.Context, unified *adapter.UnifiedRequest, ch *adapter.Channel) (*http.Request, error) {
	if unified == nil {
		return nil, fmt.Errorf("unified request is nil")
	}
	model := ch.UpstreamModel
	if model == "" {
		model = unified.Model
	}
	body := map[string]any{
		"model":    model,
		"messages": unified.Messages,
		"stream":   unified.Stream,
	}
	if unified.Temperature != nil {
		body["temperature"] = *unified.Temperature
	}
	if unified.TopP != nil {
		body["top_p"] = *unified.TopP
	}
	if unified.MaxTokens != nil {
		body["max_tokens"] = *unified.MaxTokens
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
	endpoint := base + "/chat/completions"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(payload))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	if ch.APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+ch.APIKey)
	}
	return req, nil
}

// ParseResponse parses an OpenAI-compatible chat completion response.
func (a *Adapter) ParseResponse(raw []byte) (*adapter.UnifiedResponse, error) {
	var resp adapter.UnifiedResponse
	if err := json.Unmarshal(raw, &resp); err != nil {
		return nil, fmt.Errorf("decode openai response: %w", err)
	}
	if resp.Object == "" {
		resp.Object = "chat.completion"
	}
	if resp.Created == 0 {
		resp.Created = time.Now().Unix()
	}
	var rawMap map[string]any
	if err := json.Unmarshal(raw, &rawMap); err == nil {
		resp.Raw = rawMap
	}
	return &resp, nil
}

// ParseStreamChunk parses a single OpenAI SSE data line.
func (a *Adapter) ParseStreamChunk(raw []byte) (*adapter.UnifiedChunk, error) {
	text := strings.TrimSpace(string(raw))
	if text == "[DONE]" {
		return &adapter.UnifiedChunk{Done: true}, nil
	}
	var chunk adapter.UnifiedChunk
	if err := json.Unmarshal(raw, &chunk); err != nil {
		return nil, fmt.Errorf("decode openai stream chunk: %w", err)
	}
	if chunk.Object == "" {
		chunk.Object = "chat.completion.chunk"
	}
	return &chunk, nil
}

// ExtractUsage returns the Usage from a parsed OpenAI response.
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

// HealthCheck pings the upstream /models endpoint.
func (a *Adapter) HealthCheck(ctx context.Context, ch *adapter.Channel) error {
	if ch == nil || ch.BaseURL == "" {
		return fmt.Errorf("channel base_url is empty")
	}
	base := strings.TrimRight(ch.BaseURL, "/")
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, base+"/models", nil)
	if err != nil {
		return err
	}
	if ch.APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+ch.APIKey)
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

// QueryBalance is not implemented for OpenAI-compatible providers.
func (a *Adapter) QueryBalance(_ context.Context, ch *adapter.Channel) (*adapter.Balance, error) {
	return &adapter.Balance{Provider: ch.Provider, Amount: 0, Currency: "USD"}, nil
}
