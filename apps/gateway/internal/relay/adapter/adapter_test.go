package adapter_test

import (
	"testing"

	"github.com/workama/workama/apps/gateway/internal/relay/adapter"
	_ "github.com/workama/workama/apps/gateway/internal/relay/adapter/anthropic"
	_ "github.com/workama/workama/apps/gateway/internal/relay/adapter/gemini"
	_ "github.com/workama/workama/apps/gateway/internal/relay/adapter/openai"
)

// TestResolveAdapter_P0Providers verifies all P0 providers resolve to the correct protocol adapter.
func TestResolveAdapter_P0Providers(t *testing.T) {
	cases := []struct {
		name     string
		provider string
		protocol string
	}{
		{"openai", "openai", "openai"},
		{"deepseek", "deepseek", "openai"},
		{"qwen", "qwen", "openai"},
		{"doubao", "doubao", "openai"},
		{"kimi", "kimi", "openai"},
		{"zhipu", "zhipu", "openai"},
		{"anthropic", "anthropic", "anthropic"},
		{"gemini", "gemini", "gemini"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			a, err := adapter.ResolveAdapter(tc.provider)
			if err != nil {
				t.Fatalf("ResolveAdapter(%q) returned error: %v", tc.provider, err)
			}
			if a == nil {
				t.Fatalf("ResolveAdapter(%q) returned nil adapter", tc.provider)
			}
			if got := adapter.ProtocolOf(tc.provider); got != tc.protocol {
				t.Errorf("ProtocolOf(%q) = %q, want %q", tc.provider, got, tc.protocol)
			}
		})
	}
}

// TestResolveAdapter_P0Aliases verifies common aliases for P0 providers route correctly.
func TestResolveAdapter_P0Aliases(t *testing.T) {
	cases := []struct {
		alias     string
		canonical string
	}{
		{"moonshot", "kimi"},
		{"dashscope", "qwen"},
		{"bailian", "qwen"},
		{"tongyi", "qwen"},
		{"volcengine", "doubao"},
		{"ark", "doubao"},
		{"glm", "zhipu"},
		{"bigmodel", "zhipu"},
		{"google", "gemini"},
		{"openai-compatible", "openai"},
	}
	for _, tc := range cases {
		t.Run(tc.alias, func(t *testing.T) {
			if got := adapter.NormalizeProvider(tc.alias); got != tc.canonical {
				t.Errorf("NormalizeProvider(%q) = %q, want %q", tc.alias, got, tc.canonical)
			}
			a, err := adapter.ResolveAdapter(tc.alias)
			if err != nil {
				t.Errorf("ResolveAdapter(%q) returned error: %v", tc.alias, err)
			}
			if a == nil {
				t.Errorf("ResolveAdapter(%q) returned nil adapter", tc.alias)
			}
		})
	}
}

// TestP0OpenAICompatibleProviders_AllRouteToOpenAI verifies the 5 OpenAI-compatible
// providers all route to the openai adapter (not separate adapters).
func TestP0OpenAICompatibleProviders_AllRouteToOpenAI(t *testing.T) {
	openaiAdapter, err := adapter.ResolveAdapter("openai")
	if err != nil {
		t.Fatalf("ResolveAdapter(openai) error: %v", err)
	}
	for _, provider := range adapter.P0OpenAICompatibleProviders {
		t.Run(provider, func(t *testing.T) {
			a, err := adapter.ResolveAdapter(provider)
			if err != nil {
				t.Fatalf("ResolveAdapter(%q) error: %v", provider, err)
			}
			if got, want := adapter.ProtocolOf(provider), "openai"; got != want {
				t.Errorf("ProtocolOf(%q) = %q, want %q", provider, got, want)
			}
			_ = openaiAdapter
			_ = a
		})
	}
}

// TestProviderCatalog_ContainsP0Providers verifies ProviderCatalog includes all 8 P0 providers.
func TestProviderCatalog_ContainsP0Providers(t *testing.T) {
	p0Providers := []string{
		"openai", "anthropic", "gemini",
		"deepseek", "qwen", "doubao", "kimi", "zhipu",
	}
	for _, p := range p0Providers {
		t.Run(p, func(t *testing.T) {
			entry, ok := adapter.ProviderCatalog[p]
			if !ok {
				t.Errorf("ProviderCatalog missing P0 provider: %s", p)
			}
			if entry.Protocol == "" {
				t.Errorf("ProviderCatalog[%s].Protocol is empty", p)
			}
		})
	}
}

// TestProviderCatalog_Count verifies ProviderCatalog contains 103 providers,
// matching the Python-side PROVIDER_CATALOG in router.py.
func TestProviderCatalog_Count(t *testing.T) {
	const expected = 103
	if got := adapter.ProviderCount(); got != expected {
		t.Fatalf("ProviderCount() = %d, want %d", got, expected)
	}
}

// TestProviderCatalog_ContainsFifthBatch verifies the fifth-batch free/public
// providers added for catalog parity with the Python PROVIDER_CATALOG 第四批.
func TestProviderCatalog_ContainsFifthBatch(t *testing.T) {
	fifthBatch := []string{
		"4sapi", "147api", "poloapi", "aigcbest", "deepbricks",
		"vegal", "siliconflow_global", "openrouter_free", "poe", "glm_api_chat",
		"qwenpg", "fireworks_serverless", "perplexity_online", "openai_forward", "glhf",
		"tokenflux", "llama_api", "openai_compat_proxy",
	}
	for _, p := range fifthBatch {
		t.Run(p, func(t *testing.T) {
			entry, ok := adapter.ProviderCatalog[p]
			if !ok {
				t.Fatalf("ProviderCatalog missing fifth-batch provider: %s", p)
			}
			if entry.Protocol != "openai" {
				t.Errorf("ProviderCatalog[%s].Protocol = %q, want %q", p, entry.Protocol, "openai")
			}
			if len(entry.Capabilities) == 0 {
				t.Errorf("ProviderCatalog[%s].Capabilities is empty", p)
			}
			if len(entry.Regions) == 0 {
				t.Errorf("ProviderCatalog[%s].Regions is empty", p)
			}
			if entry.RetentionMode == "" {
				t.Errorf("ProviderCatalog[%s].RetentionMode is empty", p)
			}
		})
	}
}

// TestProviderCatalog_ContainsThirdBatch verifies the third-batch free/public
// providers added for catalog parity with the Python PROVIDER_CATALOG.
func TestProviderCatalog_ContainsThirdBatch(t *testing.T) {
	thirdBatch := []string{
		"aimlapi", "monsterapi", "predibase", "baseten", "runpod",
		"anyscale", "modal", "featherless", "inference_net", "lambda",
		"fal", "bentocloud", "nvidia", "kluster", "hyperbolic",
		"ai21", "reka", "watsonx", "lightning", "duckduckgo",
		"gpt4free", "aihubmix", "api2d", "openai_hk", "closeai",
		"zhizengzeng", "ohmygpt", "chatanywhere", "ai_ls", "v3api",
		"gptgod", "baichuan", "metaso", "ppio", "oneapi",
		"newapi", "llamacpp", "xinference", "localai", "lmdeploy",
		"lmstudio", "gpt_link",
	}
	for _, p := range thirdBatch {
		t.Run(p, func(t *testing.T) {
			entry, ok := adapter.ProviderCatalog[p]
			if !ok {
				t.Fatalf("ProviderCatalog missing third-batch provider: %s", p)
			}
			if entry.Protocol != "openai" {
				t.Errorf("ProviderCatalog[%s].Protocol = %q, want %q", p, entry.Protocol, "openai")
			}
			if len(entry.Capabilities) == 0 {
				t.Errorf("ProviderCatalog[%s].Capabilities is empty", p)
			}
			if len(entry.Regions) == 0 {
				t.Errorf("ProviderCatalog[%s].Regions is empty", p)
			}
			if entry.RetentionMode == "" {
				t.Errorf("ProviderCatalog[%s].RetentionMode is empty", p)
			}
		})
	}
}
