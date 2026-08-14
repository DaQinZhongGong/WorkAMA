package adapter

import (
	"context"
	"io"
	"strings"
	"testing"
)

func TestLoadStagingConfigReturnsNilWhenMissing(t *testing.T) {
	for _, key := range []string{stagingProviderEnv, stagingAPIKeyEnv, stagingBaseURLEnv, stagingModelEnv} {
		t.Setenv(key, "")
	}
	if cfg := LoadStagingConfig(); cfg != nil {
		t.Fatalf("expected nil when credentials are missing, got %#v", cfg)
	}
}

func TestLoadStagingConfigReadsOpenAI(t *testing.T) {
	t.Setenv(stagingProviderEnv, "openai")
	t.Setenv(stagingAPIKeyEnv, "sk-test-key")
	t.Setenv(stagingBaseURLEnv, "https://staging.example/v1")
	t.Setenv(stagingModelEnv, "gpt-4o")

	cfg := LoadStagingConfig()
	if cfg == nil {
		t.Fatal("expected config, got nil")
	}
	if cfg.Provider != "openai" || cfg.APIKey != "sk-test-key" || cfg.BaseURL != "https://staging.example/v1" || cfg.Model != "gpt-4o" || !cfg.Enabled {
		t.Fatalf("unexpected config: %#v", cfg)
	}
}

func TestLoadStagingConfigUnsupportedProviderFallsBack(t *testing.T) {
	t.Setenv(stagingProviderEnv, "unknown-provider")
	t.Setenv(stagingAPIKeyEnv, "sk-test-key")
	if cfg := LoadStagingConfig(); cfg != nil {
		t.Fatalf("expected nil for unsupported provider, got %#v", cfg)
	}
}

func TestStagingAdapterBuildsOpenAIRequest(t *testing.T) {
	a, err := Resolve("staging")
	if err != nil {
		t.Fatal(err)
	}
	req, err := a.BuildChatRequest(context.Background(), Channel{
		Provider: "staging",
		BaseURL:  "https://api.openai.com/v1",
		APIKey:   "sk-secret",
		Model:    "gpt-4o-mini",
	}, map[string]any{
		"model":    "workama-chat",
		"messages": []any{map[string]any{"role": "user", "content": "hello"}},
		"stream":   false,
	})
	if err != nil {
		t.Fatal(err)
	}
	if req.URL.String() != "https://api.openai.com/v1/chat/completions" {
		t.Fatalf("unexpected url: %s", req.URL.String())
	}
	if got := req.Header.Get("Authorization"); got != "Bearer sk-secret" {
		t.Fatalf("unexpected authorization: %q", got)
	}
	body, _ := io.ReadAll(req.Body)
	if strings.Contains(string(body), "sk-secret") {
		t.Fatal("api key leaked into request body")
	}
	if !strings.Contains(string(body), `"model":"workama-chat"`) {
		t.Fatalf("unexpected body: %s", body)
	}
}

func TestStagingAdapterParsesResponse(t *testing.T) {
	a, _ := Resolve("staging")
	raw := []byte(`{"id":"chatcmpl-1","model":"upstream","choices":[{"message":{"role":"assistant","content":"hi"}}],"usage":{"completion_tokens":1}}`)
	parsed, err := a.ParseChatResponse(raw, "workama-chat")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(parsed), `"model":"workama-chat"`) {
		t.Fatalf("caller model not restored: %s", parsed)
	}
}

func TestStagingAdapterParsesStreamChunks(t *testing.T) {
	a, _ := Resolve("staging")
	parsed, done, err := a.ParseStreamChunk([]byte(`{"id":"c1","model":"upstream","choices":[{"delta":{"content":"hi"}}]}`), "workama-chat")
	if err != nil || done {
		t.Fatalf("err=%v done=%t", err, done)
	}
	if !strings.Contains(string(parsed), `"model":"workama-chat"`) {
		t.Fatalf("caller model not restored in stream: %s", parsed)
	}

	doneBytes, done, err := a.ParseStreamChunk([]byte("[DONE]"), "workama-chat")
	if err != nil || !done || string(doneBytes) != "[DONE]" {
		t.Fatalf("done parsing failed: %s %t %v", doneBytes, done, err)
	}
}
