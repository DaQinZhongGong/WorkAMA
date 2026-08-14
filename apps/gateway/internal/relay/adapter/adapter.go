// Package adapter 定义统一适配器接口与供应商协议目录。
//
// 本包提供 7 方法的 Adapter 接口与 Unified* 类型，用于在 10 步管道中
// 将 OpenAI 兼容请求转换为上游协议（OpenAI / Anthropic / Gemini）并解析
// 响应、流块与用量。ProviderCatalog 列出 103 个供应商的协议映射，
// 与 Python router.py 中的 PROVIDER_CATALOG 一致。
package adapter

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"sync"
)

// Channel 描述一个上游渠道，从 gw_channel 表读取后填充。
type Channel struct {
	ID            string   `json:"id"`
	WorkspaceID   string   `json:"workspace_id"`
	Provider      string   `json:"provider"`
	Protocol      string   `json:"protocol"`
	BaseURL       string   `json:"base_url"`
	APIKey        string   `json:"api_key"`
	Weight        int      `json:"weight"`
	Models        []string `json:"models"`
	UpstreamModel string   `json:"upstream_model"`
	Status        string   `json:"status"`
	PinnedChannel string   `json:"pinned_channel_id,omitempty"`
}

// CapabilityProfile 描述一个供应商的能力画像，对应 Python 端的 catalog 条目。
type CapabilityProfile struct {
	Provider       string   `json:"provider"`
	Protocol       string   `json:"protocol"`
	Version        string   `json:"version"`
	Capabilities   []string `json:"capabilities"`
	Regions        []string `json:"regions"`
	RetentionMode  string   `json:"retention_mode"`
	NativeProtocol string   `json:"native_protocol"`
}

// UnifiedRequest 是管道内统一传递的请求载荷（OpenAI 兼容结构）。
type UnifiedRequest struct {
	Model       string         `json:"model"`
	Messages    []Message      `json:"messages"`
	Stream      bool           `json:"stream,omitempty"`
	Temperature *float64       `json:"temperature,omitempty"`
	TopP        *float64       `json:"top_p,omitempty"`
	MaxTokens   *int           `json:"max_tokens,omitempty"`
	Tools       []any          `json:"tools,omitempty"`
	ToolChoice  any            `json:"tool_choice,omitempty"`
	Extra       map[string]any `json:"-,omitempty"`
}

// Message 兼容 OpenAI ChatCompletion 的消息结构。
type Message struct {
	Role       string `json:"role"`
	Content    any    `json:"content"`
	ToolCallID string `json:"tool_call_id,omitempty"`
	Name       string `json:"name,omitempty"`
	ToolCalls  any    `json:"tool_calls,omitempty"`
}

// UnifiedResponse 是适配器 ParseResponse 返回的统一响应（OpenAI 兼容）。
type UnifiedResponse struct {
	ID      string         `json:"id"`
	Object  string         `json:"object"`
	Created int64          `json:"created"`
	Model   string         `json:"model"`
	Choices []Choice       `json:"choices"`
	Usage   *Usage         `json:"usage,omitempty"`
	Raw     map[string]any `json:"-"`
}

// Choice 是 UnifiedResponse 中的一个候选答案。
type Choice struct {
	Index        int     `json:"index"`
	Message      Message `json:"message"`
	FinishReason string  `json:"finish_reason"`
}

// UnifiedChunk 是适配器 ParseStreamChunk 返回的统一 SSE 块（OpenAI 兼容）。
type UnifiedChunk struct {
	ID      string         `json:"id"`
	Object  string         `json:"object"`
	Created int64          `json:"created"`
	Model   string         `json:"model"`
	Choices []ChunkChoice  `json:"choices"`
	Usage   *Usage         `json:"usage,omitempty"`
	Done    bool           `json:"-"`
	Raw     map[string]any `json:"-"`
}

// ChunkChoice 是 UnifiedChunk 中的一个流式候选。
type ChunkChoice struct {
	Index        int     `json:"index"`
	Delta        Message `json:"delta"`
	FinishReason *string `json:"finish_reason"`
}

// Usage 是统一的 token 用量结构。
type Usage struct {
	PromptTokens     int `json:"prompt_tokens"`
	CompletionTokens int `json:"completion_tokens"`
	TotalTokens      int `json:"total_tokens"`
}

// Balance 是上游账户余额查询结果。
type Balance struct {
	Provider string  `json:"provider"`
	Amount   float64 `json:"amount"`
	Currency string  `json:"currency"`
}

// Adapter 定义 7 方法适配器接口（来源：《500-LLM网关设计.md》）。
type Adapter interface {
	DescribeCapabilities(ctx context.Context, ch *Channel) (*CapabilityProfile, error)
	BuildRequest(ctx context.Context, unified *UnifiedRequest, ch *Channel) (*http.Request, error)
	ParseResponse(raw []byte) (*UnifiedResponse, error)
	ParseStreamChunk(raw []byte) (*UnifiedChunk, error)
	ExtractUsage(resp *UnifiedResponse) *Usage
	HealthCheck(ctx context.Context, ch *Channel) error
	QueryBalance(ctx context.Context, ch *Channel) (*Balance, error)
}

// providerEntry 描述 ProviderCatalog 中的一个供应商条目。
type providerEntry struct {
	Protocol      string
	Capabilities  []string
	Regions       []string
	RetentionMode string
	Version       string
}

// ProviderCatalog 列出 103 个供应商的协议映射，与 Python 端 PROVIDER_CATALOG 一致。
var ProviderCatalog = map[string]providerEntry{
	// --- 第一批：主流商业供应商 ---------------------------------------------
	"openai":    {"openai", []string{"chat", "vision", "tool_call", "json_mode", "embedding", "reasoning", "background"}, []string{"global", "us", "eu"}, "provider_retained", "2024-10"},
	"anthropic": {"anthropic", []string{"chat", "vision", "tool_call", "reasoning"}, []string{"global", "us", "eu"}, "provider_retained", "2023-06-01"},
	"gemini":    {"gemini", []string{"chat", "vision", "tool_call", "json_mode", "embedding", "reasoning"}, []string{"global", "us", "eu", "asia"}, "provider_retained", "v1beta"},
	"deepseek":  {"openai", []string{"chat", "tool_call", "json_mode", "reasoning"}, []string{"cn", "global"}, "provider_retained", "2024-08"},
	"qwen":      {"openai", []string{"chat", "vision", "tool_call", "json_mode", "embedding", "reasoning"}, []string{"cn", "sg"}, "provider_retained", "2024-09"},
	"doubao":    {"openai", []string{"chat", "vision", "tool_call", "json_mode", "embedding"}, []string{"cn"}, "provider_retained", "2024-07"},
	"kimi":      {"openai", []string{"chat", "vision", "tool_call", "json_mode", "reasoning"}, []string{"cn"}, "provider_retained", "2024-10"},
	"zhipu":     {"openai", []string{"chat", "vision", "tool_call", "json_mode", "embedding"}, []string{"cn"}, "provider_retained", "2024-08"},
	"ollama":    {"openai", []string{"chat", "vision", "tool_call", "embedding"}, []string{"self_hosted"}, "ephemeral_retention", "0.9"},
	"vllm":      {"openai", []string{"chat", "vision", "tool_call", "json_mode", "embedding"}, []string{"self_hosted"}, "ephemeral_retention", "0.6"},
	"azure":     {"openai", []string{"chat", "vision", "tool_call", "json_mode", "embedding", "reasoning"}, []string{"global", "us", "eu"}, "provider_retained", "2024-10-21"},
	"bedrock":   {"openai", []string{"chat", "vision", "tool_call", "reasoning"}, []string{"global", "us", "eu", "asia"}, "provider_retained", "2024-09"},
	"minimax":   {"openai", []string{"chat", "vision", "tool_call", "reasoning"}, []string{"cn", "global"}, "provider_retained", "2024-07"},
	"qianfan":   {"openai", []string{"chat", "vision", "tool_call", "embedding"}, []string{"cn"}, "provider_retained", "2024-08"},
	"hunyuan":   {"openai", []string{"chat", "vision", "tool_call", "json_mode"}, []string{"cn"}, "provider_retained", "2024-09"},
	"mistral":   {"openai", []string{"chat", "vision", "tool_call", "json_mode", "embedding", "reasoning"}, []string{"global", "eu"}, "provider_retained", "2024-10"},
	"xai":       {"openai", []string{"chat", "vision", "tool_call", "reasoning"}, []string{"global", "us"}, "provider_retained", "2024-10"},
	// --- 第二批：免费 / 公益大模型供应商 -------------------------------------
	"siliconflow":  {"openai", []string{"chat", "vision", "tool_call", "json_mode", "embedding", "reasoning"}, []string{"cn", "global"}, "provider_retained", "2024-09"},
	"openrouter":   {"openai", []string{"chat", "vision", "tool_call", "json_mode", "reasoning"}, []string{"global", "us", "eu"}, "provider_retained", "2024-10"},
	"groq":         {"openai", []string{"chat", "tool_call", "json_mode", "reasoning"}, []string{"global", "us"}, "provider_retained", "2024-09"},
	"together":     {"openai", []string{"chat", "vision", "tool_call", "json_mode", "embedding", "reasoning"}, []string{"global", "us"}, "provider_retained", "2024-09"},
	"cerebras":     {"openai", []string{"chat", "tool_call", "json_mode", "reasoning"}, []string{"global", "us"}, "provider_retained", "2024-09"},
	"cloudflare":   {"openai", []string{"chat", "tool_call", "embedding"}, []string{"global"}, "provider_retained", "2024-09"},
	"huggingface":  {"openai", []string{"chat", "embedding"}, []string{"global", "eu"}, "provider_retained", "2024-09"},
	"modelscope":   {"openai", []string{"chat", "vision", "embedding"}, []string{"cn"}, "provider_retained", "2024-09"},
	"iflytek":      {"openai", []string{"chat", "vision", "tool_call"}, []string{"cn"}, "provider_retained", "2024-08"},
	"dmxapi":       {"openai", []string{"chat", "vision", "tool_call", "json_mode", "embedding", "reasoning"}, []string{"cn", "global"}, "provider_retained", "2024-09"},
	"n1n":          {"openai", []string{"chat", "vision", "tool_call", "json_mode", "reasoning"}, []string{"global", "us"}, "provider_retained", "2024-09"},
	"github":       {"openai", []string{"chat", "vision", "tool_call", "json_mode", "reasoning"}, []string{"global", "us"}, "provider_retained", "2024-10"},
	"perplexity":   {"openai", []string{"chat", "tool_call", "json_mode"}, []string{"global", "us"}, "provider_retained", "2024-09"},
	"cohere":       {"openai", []string{"chat", "tool_call", "json_mode", "embedding", "reasoning"}, []string{"global", "us"}, "provider_retained", "2024-09"},
	"mistral_free": {"openai", []string{"chat", "vision", "tool_call", "json_mode", "embedding", "reasoning"}, []string{"global", "eu"}, "provider_retained", "2024-09"},
	// --- 第三批：开源模型推理 / 聚合 / 国内免费层 ---------------------------
	"deepinfra": {"openai", []string{"chat", "vision", "tool_call", "json_mode", "embedding"}, []string{"global", "us"}, "provider_retained", "2024-09"},
	"fireworks": {"openai", []string{"chat", "vision", "tool_call", "json_mode", "embedding", "reasoning"}, []string{"global", "us"}, "provider_retained", "2024-09"},
	"novita":    {"openai", []string{"chat", "vision", "tool_call", "json_mode", "embedding"}, []string{"global", "us"}, "provider_retained", "2024-09"},
	"lepton":    {"openai", []string{"chat", "vision", "tool_call", "json_mode"}, []string{"global", "us"}, "provider_retained", "2024-09"},
	"replicate": {"openai", []string{"chat", "vision"}, []string{"global", "us"}, "provider_retained", "2024-09"},
	"stepfun":   {"openai", []string{"chat", "vision", "tool_call"}, []string{"cn"}, "provider_retained", "2024-09"},
	"lingyi":    {"openai", []string{"chat", "vision", "tool_call", "json_mode"}, []string{"cn", "global"}, "provider_retained", "2024-09"},
	"sambanova": {"openai", []string{"chat", "tool_call", "json_mode"}, []string{"global", "us"}, "provider_retained", "2024-09"},
	"chutes":    {"openai", []string{"chat", "tool_call", "json_mode", "reasoning"}, []string{"global", "us"}, "provider_retained", "2024-09"},
	"nebius":    {"openai", []string{"chat", "tool_call", "json_mode", "embedding"}, []string{"global", "eu"}, "provider_retained", "2024-09"},
	"openbayes": {"openai", []string{"chat", "tool_call"}, []string{"cn"}, "provider_retained", "2024-09"},
	// --- 第四批：聚合 / 自部署 / 国内国际免费层 -----------------------------
	"aimlapi":       {"openai", []string{"chat", "vision", "tool_call", "json_mode"}, []string{"global"}, "provider_retained", "2024-09"},
	"monsterapi":    {"openai", []string{"chat", "tool_call", "json_mode"}, []string{"global"}, "provider_retained", "2024-09"},
	"predibase":     {"openai", []string{"chat", "tool_call", "json_mode"}, []string{"global"}, "provider_retained", "2024-09"},
	"baseten":       {"openai", []string{"chat", "tool_call"}, []string{"global"}, "provider_retained", "2024-09"},
	"runpod":        {"openai", []string{"chat"}, []string{"global"}, "provider_retained", "2024-09"},
	"anyscale":      {"openai", []string{"chat", "tool_call", "json_mode"}, []string{"global"}, "provider_retained", "2024-09"},
	"modal":         {"openai", []string{"chat"}, []string{"global"}, "provider_retained", "2024-09"},
	"featherless":   {"openai", []string{"chat", "tool_call"}, []string{"global"}, "provider_retained", "2024-09"},
	"inference_net": {"openai", []string{"chat", "tool_call"}, []string{"global"}, "provider_retained", "2024-09"},
	"lambda":        {"openai", []string{"chat", "tool_call", "json_mode"}, []string{"global"}, "provider_retained", "2024-09"},
	"fal":           {"openai", []string{"chat", "vision"}, []string{"global"}, "provider_retained", "2024-09"},
	"bentocloud":    {"openai", []string{"chat"}, []string{"global"}, "provider_retained", "2024-09"},
	"nvidia":        {"openai", []string{"chat", "tool_call", "json_mode", "vision"}, []string{"global"}, "provider_retained", "2024-09"},
	"kluster":       {"openai", []string{"chat", "reasoning", "tool_call"}, []string{"global"}, "provider_retained", "2024-09"},
	"hyperbolic":    {"openai", []string{"chat", "reasoning"}, []string{"global"}, "provider_retained", "2024-09"},
	"ai21":          {"openai", []string{"chat", "tool_call"}, []string{"global"}, "provider_retained", "2024-09"},
	"reka":          {"openai", []string{"chat", "vision", "reasoning"}, []string{"global"}, "provider_retained", "2024-09"},
	"watsonx":       {"openai", []string{"chat", "tool_call"}, []string{"global"}, "provider_retained", "2024-09"},
	"lightning":     {"openai", []string{"chat"}, []string{"global"}, "provider_retained", "2024-09"},
	"duckduckgo":    {"openai", []string{"chat", "reasoning"}, []string{"global"}, "provider_retained", "2024-09"},
	"gpt4free":      {"openai", []string{"chat"}, []string{"self_hosted"}, "ephemeral_retention", "2024-09"},
	"aihubmix":      {"openai", []string{"chat", "vision", "tool_call", "json_mode"}, []string{"cn"}, "provider_retained", "2024-09"},
	"api2d":         {"openai", []string{"chat", "vision", "tool_call", "json_mode"}, []string{"cn"}, "provider_retained", "2024-09"},
	"openai_hk":     {"openai", []string{"chat", "vision", "tool_call", "json_mode"}, []string{"cn"}, "provider_retained", "2024-09"},
	"closeai":       {"openai", []string{"chat", "vision", "tool_call", "json_mode"}, []string{"cn"}, "provider_retained", "2024-09"},
	"zhizengzeng":   {"openai", []string{"chat", "vision", "tool_call", "json_mode"}, []string{"cn"}, "provider_retained", "2024-09"},
	"ohmygpt":       {"openai", []string{"chat", "vision", "tool_call"}, []string{"cn"}, "provider_retained", "2024-09"},
	"chatanywhere":  {"openai", []string{"chat", "vision", "tool_call"}, []string{"cn"}, "provider_retained", "2024-09"},
	"ai_ls":         {"openai", []string{"chat", "vision", "tool_call", "json_mode"}, []string{"global"}, "provider_retained", "2024-09"},
	"v3api":         {"openai", []string{"chat", "vision", "tool_call", "json_mode"}, []string{"cn"}, "provider_retained", "2024-09"},
	"gptgod":        {"openai", []string{"chat", "vision", "tool_call", "json_mode"}, []string{"cn"}, "provider_retained", "2024-09"},
	"baichuan":      {"openai", []string{"chat", "tool_call", "json_mode"}, []string{"cn"}, "provider_retained", "2024-09"},
	"metaso":        {"openai", []string{"chat", "tool_call"}, []string{"cn"}, "provider_retained", "2024-09"},
	"ppio":          {"openai", []string{"chat", "reasoning", "tool_call"}, []string{"cn"}, "provider_retained", "2024-09"},
	"oneapi":        {"openai", []string{"chat", "vision", "tool_call", "json_mode"}, []string{"self_hosted"}, "ephemeral_retention", "2024-09"},
	"newapi":        {"openai", []string{"chat", "vision", "tool_call", "json_mode"}, []string{"self_hosted"}, "ephemeral_retention", "2024-09"},
	"llamacpp":      {"openai", []string{"chat", "tool_call"}, []string{"self_hosted"}, "ephemeral_retention", "2024-09"},
	"xinference":    {"openai", []string{"chat", "vision", "tool_call", "json_mode"}, []string{"self_hosted"}, "ephemeral_retention", "2024-09"},
	"localai":       {"openai", []string{"chat", "vision", "tool_call", "json_mode"}, []string{"self_hosted"}, "ephemeral_retention", "2024-09"},
	"lmdeploy":      {"openai", []string{"chat", "tool_call"}, []string{"self_hosted"}, "ephemeral_retention", "2024-09"},
	"lmstudio":      {"openai", []string{"chat", "tool_call", "json_mode"}, []string{"self_hosted"}, "ephemeral_retention", "2024-09"},
	"gpt_link":      {"openai", []string{"chat", "vision", "tool_call", "json_mode"}, []string{"cn"}, "provider_retained", "2024-09"},
	// --- 第五批：聚合 / 国内国际免费层扩展（与 Python router.py 第四批对齐）---
	"4sapi":                {"openai", []string{"chat", "vision", "tool_call", "json_mode"}, []string{"cn", "global"}, "provider_retained", "2024-09"},
	"147api":               {"openai", []string{"chat", "vision", "tool_call", "json_mode"}, []string{"cn", "global"}, "provider_retained", "2024-09"},
	"poloapi":              {"openai", []string{"chat", "vision", "tool_call", "json_mode"}, []string{"cn", "global"}, "provider_retained", "2024-09"},
	"aigcbest":             {"openai", []string{"chat", "vision", "tool_call", "json_mode"}, []string{"cn", "global"}, "provider_retained", "2024-09"},
	"deepbricks":           {"openai", []string{"chat", "vision", "tool_call", "json_mode"}, []string{"global", "us"}, "provider_retained", "2024-09"},
	"vegal":                {"openai", []string{"chat", "vision", "tool_call", "json_mode"}, []string{"cn", "global"}, "provider_retained", "2024-09"},
	"siliconflow_global":   {"openai", []string{"chat", "vision", "tool_call", "json_mode", "embedding"}, []string{"global", "us", "sg"}, "provider_retained", "2024-09"},
	"openrouter_free":      {"openai", []string{"chat", "vision", "tool_call", "json_mode", "reasoning"}, []string{"global", "us", "eu"}, "provider_retained", "2024-09"},
	"poe":                  {"openai", []string{"chat", "vision"}, []string{"global", "us"}, "provider_retained", "2024-09"},
	"glm_api_chat":         {"openai", []string{"chat", "vision", "tool_call", "json_mode", "embedding"}, []string{"cn"}, "provider_retained", "2024-09"},
	"qwenpg":               {"openai", []string{"chat", "vision", "tool_call", "json_mode", "embedding", "reasoning"}, []string{"cn", "sg"}, "provider_retained", "2024-09"},
	"fireworks_serverless": {"openai", []string{"chat", "vision", "tool_call", "json_mode", "reasoning"}, []string{"global", "us"}, "provider_retained", "2024-09"},
	"perplexity_online":    {"openai", []string{"chat", "tool_call", "json_mode"}, []string{"global", "us"}, "provider_retained", "2024-09"},
	"openai_forward":       {"openai", []string{"chat", "vision", "tool_call", "json_mode"}, []string{"self_hosted"}, "provider_retained", "2024-09"},
	"glhf":                 {"openai", []string{"chat", "tool_call", "json_mode"}, []string{"global", "us"}, "provider_retained", "2024-09"},
	"tokenflux":            {"openai", []string{"chat", "tool_call", "json_mode"}, []string{"global"}, "provider_retained", "2024-09"},
	"llama_api":            {"openai", []string{"chat", "vision", "tool_call", "json_mode", "reasoning"}, []string{"global", "us"}, "provider_retained", "2024-09"},
	"openai_compat_proxy":  {"openai", []string{"chat", "vision", "tool_call", "json_mode"}, []string{"self_hosted"}, "provider_retained", "2024-09"},
}

// ProviderAliases 与 Python 端 PROVIDER_ALIASES 一致，用于将别名归一化为标准供应商名。
var ProviderAliases = map[string]string{
	"openai-compatible": "openai",
	"google":            "gemini",
	"google-gemini":     "gemini",
	"dashscope":         "qwen",
	"bailian":           "qwen",
	"tongyi":            "qwen",
	"volcengine":        "doubao",
	"ark":               "doubao",
	"moonshot":          "kimi",
	"glm":               "zhipu",
	"bigmodel":          "zhipu",
	"azure-openai":      "azure",
	"azure_openai":      "azure",
	"amazon-bedrock":    "bedrock",
	"self-hosted":       "vllm",
	"硅基流动":              "siliconflow",
	"silicon":           "siliconflow",
	"open-router":       "openrouter",
	"groq-cloud":        "groq",
	"together-ai":       "together",
	"workers-ai":        "cloudflare",
	"hf":                "huggingface",
	"hugging-face":      "huggingface",
	"魔搭":                "modelscope",
	"model-scope":       "modelscope",
	"星火":                "iflytek",
	"spark":             "iflytek",
	"讯飞":                "iflytek",
	"github-models":     "github",
	"perplexity-ai":     "perplexity",
	"cohere-ai":         "cohere",
	"deep-infra":        "deepinfra",
	"fireworks-ai":      "fireworks",
	"novita-ai":         "novita",
	"lepton-ai":         "lepton",
	"阶跃":                "stepfun",
	"阶跃星辰":              "stepfun",
	"零一":                "lingyi",
	"01-ai":             "lingyi",
	"01.ai":             "lingyi",
	"yi":                "lingyi",
	"samba-nova":        "sambanova",
	"chutes-ai":         "chutes",
	"nebius-ai":         "nebius",
	"贝式计算":              "openbayes",
	"open-bayes":        "openbayes",
}

// NormalizeProvider 将供应商别名归一化为 ProviderCatalog 中的标准名。
func NormalizeProvider(value string) string {
	if value == "" {
		return "openai"
	}
	normalized := strings.ToLower(strings.TrimSpace(value))
	if canonical, ok := ProviderAliases[normalized]; ok {
		return canonical
	}
	return normalized
}

// ProtocolOf 返回某供应商对应的协议（openai / anthropic / gemini）。
// 未识别的供应商默认按 openai 协议处理。
func ProtocolOf(provider string) string {
	provider = NormalizeProvider(provider)
	if entry, ok := ProviderCatalog[provider]; ok {
		return entry.Protocol
	}
	return "openai"
}

// DescribeCapabilities 返回 ProviderCatalog 中供应商的能力画像。
func DescribeCapabilities(provider string) *CapabilityProfile {
	provider = NormalizeProvider(provider)
	entry, ok := ProviderCatalog[provider]
	if !ok {
		entry = providerEntry{Protocol: "openai", Capabilities: []string{"chat"}, Regions: []string{"global"}, RetentionMode: "provider_retained", Version: "2024-10"}
	}
	profile := &CapabilityProfile{
		Provider:       provider,
		Protocol:       entry.Protocol,
		Version:        entry.Version,
		Capabilities:   entry.Capabilities,
		Regions:        entry.Regions,
		RetentionMode:  entry.RetentionMode,
		NativeProtocol: entry.Protocol,
	}
	return profile
}

// ProviderCount 返回 ProviderCatalog 中的供应商数量（用于测试断言）。
func ProviderCount() int { return len(ProviderCatalog) }

// AdapterFactory 是构造 Adapter 的工厂函数类型。
type AdapterFactory func(provider string) (Adapter, error)

// 工厂注册表：由各协议子包（openai/anthropic/gemini）在 init() 中注册。
// 采用注册表模式避免 adapter 包反向依赖子包导致的循环导入；
// 调用方（main.go）通过 blank import 触发子包 init() 完成注册。
var (
	factoryMu sync.RWMutex
	factories = map[string]AdapterFactory{}
)

// RegisterFactory 注册某协议的适配器工厂。
// 由 adapter/openai、adapter/anthropic、adapter/gemini 子包在 init() 中调用。
// 协议名应与 ProviderCatalog 中的 Protocol 字段一致（openai/anthropic/gemini）。
func RegisterFactory(protocol string, factory AdapterFactory) {
	factoryMu.Lock()
	defer factoryMu.Unlock()
	if factories == nil {
		factories = map[string]AdapterFactory{}
	}
	factories[protocol] = factory
}

// errUnknownProvider 是 ResolveAdapter 返回的错误，包内复用。
var errUnknownProvider = fmt.Errorf("unsupported provider: no adapter factory registered")

// P0OpenAICompatibleProviders 列出 P0 首批 8 家供应商中使用 OpenAI 兼容协议
// 的 5 家：DeepSeek / Qwen（百炼）/ 豆包（火山方舟）/ Kimi（月之暗面）/ 智谱 GLM。
// 这些供应商复用 openai 适配器（adapter/openai）的全部逻辑：
//   - BuildRequest       直接转发 OpenAI ChatCompletion 结构，仅替换 model
//   - ParseResponse      原样返回 OpenAI 响应
//   - ParseStreamChunk   原样解析 OpenAI SSE chunk
//   - ExtractUsage       原样读取 usage 字段
//
// 各供应商的差异仅在 Channel.BaseURL 与 Channel.UpstreamModel（由 gw_channel 表
// 配置，不在适配器内硬编码）：
//
//	deepseek  https://api.deepseek.com/v1
//	qwen      https://dashscope.aliyuncs.com/compatible-mode/v1
//	doubao    https://ark.cn-beijing.volces.com/api/v3
//	kimi      https://api.moonshot.cn/v1
//	zhipu     https://open.bigmodel.cn/api/paas/v4
//
// 与《500-LLM网关设计.md》§3 P0 首批 8 家供应商清单一致。
var P0OpenAICompatibleProviders = []string{
	"deepseek", "qwen", "doubao", "kimi", "zhipu",
}

// isOpenAICompatibleProvider 判断供应商是否在 P0 OpenAI 兼容协议清单中。
// 该判断仅用于在 ResolveAdapter 中给出更精确的错误信息，不影响路由结果——
// 这些供应商在 ProviderCatalog 中已映射到 protocol="openai"。
func isOpenAICompatibleProvider(provider string) bool {
	for _, name := range P0OpenAICompatibleProviders {
		if name == provider {
			return true
		}
	}
	return false
}

// ResolveAdapter 根据供应商名解析对应的 Adapter 实现。
//
// 路由策略：
//  1. NormalizeProvider 将别名归一化为 ProviderCatalog 中的标准名
//     （如 "moonshot" → "kimi"，"dashscope" → "qwen"）。
//  2. ProtocolOf 查 ProviderCatalog 得到协议（openai/anthropic/gemini）。
//  3. 从工厂注册表中取出对应协议的工厂并调用，返回 Adapter 实例。
//
// P0 首批 8 家供应商的路由结果：
//   - openai / deepseek / qwen / doubao / kimi / zhipu → openai 适配器
//   - anthropic                                          → anthropic 适配器
//   - gemini                                             → gemini 适配器
//
// 5 家 OpenAI 兼容供应商（deepseek/qwen/doubao/kimi/zhipu）复用 openai 适配器，
// 仅 Channel.BaseURL / Channel.UpstreamModel 不同。ProviderCatalog 中已将它们
// 全部映射到 protocol="openai"，因此无需为每家单独创建子包。
//
// 若该协议尚未注册工厂（调用方未 blank import 子包），返回 errUnknownProvider。
// 不支持的供应商将回退为 openai 协议处理（保持与 Python 行为一致）。
func ResolveAdapter(provider string) (Adapter, error) {
	provider = NormalizeProvider(provider)
	protocol := ProtocolOf(provider)
	factoryMu.RLock()
	factory, ok := factories[protocol]
	factoryMu.RUnlock()
	if !ok || factory == nil {
		hint := fmt.Sprintf("protocol=%s provider=%s (import the adapter subpackage)", protocol, provider)
		if isOpenAICompatibleProvider(provider) {
			hint = fmt.Sprintf("protocol=openai provider=%s (import adapter/openai subpackage)", provider)
		}
		return nil, fmt.Errorf("%w: %s", errUnknownProvider, hint)
	}
	return factory(provider)
}
