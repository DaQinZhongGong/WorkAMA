package adapter

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"os"
	"strings"
)

// StagingConfig holds optional real-LLM staging overrides loaded from the
// environment. When the configuration is absent the gateway keeps the existing
// mock/local deterministic behaviour.
type StagingConfig struct {
	Provider string // e.g. "openai" or "openai-compatible"
	APIKey   string
	BaseURL  string
	Model    string
	Enabled  bool
}

const (
	stagingProviderEnv = "LLM_STAGING_PROVIDER"
	stagingAPIKeyEnv   = "LLM_STAGING_API_KEY"
	stagingBaseURLEnv  = "LLM_STAGING_BASE_URL"
	stagingModelEnv    = "LLM_STAGING_MODEL"
)

// LoadStagingConfig reads staging credentials from the environment. It returns
// nil when the required credentials are missing or the provider is unsupported,
// which signals the caller to fall back to the existing mock/local path.
func LoadStagingConfig() *StagingConfig {
	provider := strings.ToLower(strings.TrimSpace(os.Getenv(stagingProviderEnv)))
	apiKey := strings.TrimSpace(os.Getenv(stagingAPIKeyEnv))
	if provider == "" || apiKey == "" {
		return nil
	}

	switch provider {
	case "openai", "openai-compatible", "azure", "azure_openai":
		// All handled as OpenAI-compatible chat completions for now.
		// Provider-specific protocol adapters can be added here later.
	default:
		// Unsupported staging provider; keep mock/local fallback.
		return nil
	}

	baseURL := strings.TrimSpace(os.Getenv(stagingBaseURLEnv))
	if baseURL == "" {
		baseURL = "https://api.openai.com/v1"
	}

	model := strings.TrimSpace(os.Getenv(stagingModelEnv))
	if model == "" {
		model = "gpt-4o-mini"
	}

	return &StagingConfig{
		Provider: provider,
		APIKey:   apiKey,
		BaseURL:  baseURL,
		Model:    model,
		Enabled:  true,
	}
}

// stagingAdapter implements an OpenAI-compatible chat-completion adapter for
// real-LLM staging. It supports non-streaming responses and SSE streams and is
// used only when LLM_STAGING_PROVIDER and LLM_STAGING_API_KEY are configured.
type stagingAdapter struct{}

func (stagingAdapter) DescribeCapabilities(context.Context) CapabilityProfile {
	return CapabilityProfile{
		Provider:       "staging",
		Version:        "2024-10",
		Capabilities:   []string{"chat", "vision", "tool_call", "json_mode", "reasoning"},
		Regions:        []string{"global"},
		RetentionMode:  "provider_retained",
		NativeProtocol: "openai",
	}
}

func (stagingAdapter) BuildChatRequest(ctx context.Context, ch Channel, unified map[string]any) (*http.Request, error) {
	payload, err := json.Marshal(unified)
	if err != nil {
		return nil, err
	}

	base := strings.TrimRight(ch.BaseURL, "/")
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, base+"/chat/completions", bytes.NewReader(payload))
	if err != nil {
		return nil, err
	}

	req.Header.Set("Content-Type", "application/json")
	if ch.APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+ch.APIKey)
	}
	return req, nil
}

func (stagingAdapter) ParseChatResponse(raw []byte, callerModel string) ([]byte, error) {
	return setModel(raw, callerModel), nil
}

func (stagingAdapter) ParseStreamChunk(raw []byte, callerModel string) ([]byte, bool, error) {
	if string(raw) == "[DONE]" {
		return raw, true, nil
	}
	return setModel(raw, callerModel), false, nil
}
