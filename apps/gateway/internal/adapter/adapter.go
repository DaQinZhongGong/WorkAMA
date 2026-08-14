package adapter

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strings"
)

type CapabilityProfile struct {
	Provider       string   `json:"provider"`
	Version        string   `json:"version"`
	Capabilities   []string `json:"capabilities"`
	Regions        []string `json:"regions"`
	RetentionMode  string   `json:"retention_mode"`
	NativeProtocol string   `json:"native_protocol"`
}

type Channel struct{ Provider, BaseURL, APIKey, Model string }

type Adapter interface {
	DescribeCapabilities(context.Context) CapabilityProfile
	BuildChatRequest(context.Context, Channel, map[string]any) (*http.Request, error)
	ParseChatResponse([]byte, string) ([]byte, error)
	ParseStreamChunk([]byte, string) ([]byte, bool, error)
}

var profiles = map[string]CapabilityProfile{
	"openai":    {"openai", "2026-07", []string{"chat", "vision", "tool_call", "json_mode", "embedding", "reasoning", "background"}, []string{"global", "us", "eu"}, "provider_retained", "openai"},
	"anthropic": {"anthropic", "2023-06-01", []string{"chat", "vision", "tool_call", "reasoning"}, []string{"global", "us", "eu"}, "provider_retained", "anthropic"},
	"gemini":    {"gemini", "v1beta", []string{"chat", "vision", "tool_call", "json_mode", "embedding", "reasoning"}, []string{"global", "us", "eu", "asia"}, "provider_retained", "gemini"},
	"deepseek":  {"deepseek", "2026-07", []string{"chat", "tool_call", "json_mode", "reasoning"}, []string{"cn", "global"}, "provider_retained", "openai"},
	"qwen":      {"qwen", "2026-07", []string{"chat", "vision", "tool_call", "json_mode", "embedding", "reasoning"}, []string{"cn", "sg"}, "provider_retained", "openai"},
	"doubao":    {"doubao", "2026-07", []string{"chat", "vision", "tool_call", "json_mode", "embedding"}, []string{"cn"}, "provider_retained", "openai"},
	"kimi":      {"kimi", "2026-07", []string{"chat", "vision", "tool_call", "json_mode", "reasoning"}, []string{"cn"}, "provider_retained", "openai"},
	"zhipu":     {"zhipu", "2026-07", []string{"chat", "vision", "tool_call", "json_mode", "embedding"}, []string{"cn"}, "provider_retained", "openai"},
	"ollama":    {"ollama", "0.9", []string{"chat", "vision", "tool_call", "embedding"}, []string{"self_hosted"}, "ephemeral_retention", "openai"},
	"vllm":      {"vllm", "0.6", []string{"chat", "vision", "tool_call", "json_mode", "embedding"}, []string{"self_hosted"}, "ephemeral_retention", "openai"},
	"azure":     {"azure", "2024-10-21", []string{"chat", "vision", "tool_call", "json_mode", "embedding", "reasoning"}, []string{"global", "us", "eu"}, "provider_retained", "openai"},
	"bedrock":   {"bedrock", "2026-07", []string{"chat", "vision", "tool_call", "reasoning"}, []string{"global", "us", "eu", "asia"}, "provider_retained", "openai"},
	"minimax":   {"minimax", "2026-07", []string{"chat", "vision", "tool_call", "reasoning"}, []string{"cn", "global"}, "provider_retained", "openai"},
	"qianfan":   {"qianfan", "2026-07", []string{"chat", "vision", "tool_call", "embedding"}, []string{"cn"}, "provider_retained", "openai"},
	"hunyuan":   {"hunyuan", "2026-07", []string{"chat", "vision", "tool_call", "json_mode"}, []string{"cn"}, "provider_retained", "openai"},
	"mistral":   {"mistral", "2026-07", []string{"chat", "vision", "tool_call", "json_mode", "embedding", "reasoning"}, []string{"global", "eu"}, "provider_retained", "openai"},
	"xai":       {"xai", "2026-07", []string{"chat", "vision", "tool_call", "reasoning"}, []string{"global", "us"}, "provider_retained", "openai"},
	"siliconflow": {"siliconflow", "2026-07", []string{"chat", "vision", "tool_call", "json_mode", "embedding", "reasoning"}, []string{"cn", "global"}, "provider_retained", "openai"},
}

func Providers() []string {
	return []string{"openai", "anthropic", "gemini", "deepseek", "qwen", "doubao", "kimi", "zhipu", "ollama", "vllm", "azure", "bedrock", "minimax", "qianfan", "hunyuan", "mistral", "xai", "siliconflow"}
}

func NormalizeProvider(value string) string {
	v := strings.ToLower(strings.TrimSpace(value))
	switch v {
	case "":
		return "openai"
	case "openai-compatible", "openai_compatible":
		return "openai"
	case "google", "google-gemini":
		return "gemini"
	case "dashscope", "bailian", "tongyi":
		return "qwen"
	case "volcengine", "ark":
		return "doubao"
	case "moonshot":
		return "kimi"
	case "glm", "bigmodel":
		return "zhipu"
	case "azure-openai", "azure_openai":
		return "azure"
	case "amazon-bedrock":
		return "bedrock"
	case "self-hosted":
		return "vllm"
	case "siliconflow-cn", "siliconflow_cn":
		return "siliconflow"
	case "siliconflow-global", "siliconflow_global":
		return "siliconflow"
	case "sf":
		return "siliconflow"
	}
	return v
}

func Resolve(provider string) (Adapter, error) {
	provider = NormalizeProvider(provider)
	if provider == "staging" {
		return stagingAdapter{}, nil
	}
	profile, ok := profiles[provider]
	if !ok {
		return nil, fmt.Errorf("unsupported provider %q", provider)
	}
	switch provider {
	case "anthropic":
		return nativeAdapter{profile: profile, protocol: "anthropic"}, nil
	case "gemini":
		return nativeAdapter{profile: profile, protocol: "gemini"}, nil
	default:
		return nativeAdapter{profile: profile, protocol: "openai"}, nil
	}
}

type nativeAdapter struct {
	profile  CapabilityProfile
	protocol string
}

func (a nativeAdapter) DescribeCapabilities(context.Context) CapabilityProfile { return a.profile }

func (a nativeAdapter) BuildChatRequest(ctx context.Context, ch Channel, unified map[string]any) (*http.Request, error) {
	model := ch.Model
	if model == "" {
		model, _ = unified["model"].(string)
	}
	base := strings.TrimRight(ch.BaseURL, "/")
	body := unified
	endpoint := base + "/chat/completions"
	if a.protocol == "anthropic" {
		messages, _ := unified["messages"].([]any)
		body = map[string]any{"model": model, "messages": messages, "max_tokens": valueOr(unified["max_tokens"], 1024), "stream": valueOr(unified["stream"], false)}
		endpoint = base + "/messages"
	} else if a.protocol == "gemini" {
		contents := []map[string]any{}
		if messages, ok := unified["messages"].([]any); ok {
			for _, raw := range messages {
				if m, ok := raw.(map[string]any); ok {
					role, _ := m["role"].(string)
					if role == "assistant" {
						role = "model"
					} else {
						role = "user"
					}
					contents = append(contents, map[string]any{"role": role, "parts": []map[string]any{{"text": fmt.Sprint(m["content"])}}})
				}
			}
		}
		body = map[string]any{"contents": contents, "generationConfig": map[string]any{"temperature": unified["temperature"], "maxOutputTokens": unified["max_tokens"]}}
		method := "generateContent"
		if streaming, _ := unified["stream"].(bool); streaming {
			method = "streamGenerateContent"
		}
		endpoint = base + "/models/" + url.PathEscape(model) + ":" + method + "?key=" + url.QueryEscape(ch.APIKey)
	}
	payload, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(payload))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if a.protocol == "anthropic" {
		req.Header.Set("x-api-key", ch.APIKey)
		req.Header.Set("anthropic-version", a.profile.Version)
	} else if a.protocol == "openai" && ch.APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+ch.APIKey)
	}
	return req, nil
}

func (a nativeAdapter) ParseChatResponse(raw []byte, callerModel string) ([]byte, error) {
	if a.protocol == "openai" {
		return setModel(raw, callerModel), nil
	}
	var source map[string]any
	if err := json.Unmarshal(raw, &source); err != nil {
		return nil, err
	}
	text, finish := "", "stop"
	prompt, completion := 0, 0
	if a.protocol == "anthropic" {
		if blocks, ok := source["content"].([]any); ok {
			for _, rawBlock := range blocks {
				if block, ok := rawBlock.(map[string]any); ok && block["type"] == "text" {
					text += fmt.Sprint(block["text"])
				}
			}
		}
		if usage, ok := source["usage"].(map[string]any); ok {
			prompt = intNumber(usage["input_tokens"])
			completion = intNumber(usage["output_tokens"])
		}
		if source["stop_reason"] == "max_tokens" {
			finish = "length"
		}
	} else {
		if candidates, ok := source["candidates"].([]any); ok && len(candidates) > 0 {
			candidate, _ := candidates[0].(map[string]any)
			if content, ok := candidate["content"].(map[string]any); ok {
				if parts, ok := content["parts"].([]any); ok {
					for _, rawPart := range parts {
						if part, ok := rawPart.(map[string]any); ok {
							text += fmt.Sprint(part["text"])
						}
					}
				}
			}
			if candidate["finishReason"] == "MAX_TOKENS" {
				finish = "length"
			}
		}
		if usage, ok := source["usageMetadata"].(map[string]any); ok {
			prompt = intNumber(usage["promptTokenCount"])
			completion = intNumber(usage["candidatesTokenCount"])
		}
	}
	return json.Marshal(map[string]any{"id": valueOr(source["id"], "chatcmpl-workama"), "object": "chat.completion", "model": callerModel, "choices": []any{map[string]any{"index": 0, "message": map[string]any{"role": "assistant", "content": text}, "finish_reason": finish}}, "usage": map[string]int{"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}})
}

func (a nativeAdapter) ParseStreamChunk(raw []byte, callerModel string) ([]byte, bool, error) {
	if string(raw) == "[DONE]" {
		return raw, true, nil
	}
	if a.protocol == "openai" {
		return setModel(raw, callerModel), false, nil
	}
	var source map[string]any
	if err := json.Unmarshal(raw, &source); err != nil {
		return nil, false, err
	}
	text := ""
	done := false
	if a.protocol == "anthropic" {
		if delta, ok := source["delta"].(map[string]any); ok {
			text = fmt.Sprint(delta["text"])
		}
		done = source["type"] == "message_stop"
	} else {
		if candidates, ok := source["candidates"].([]any); ok && len(candidates) > 0 {
			candidate, _ := candidates[0].(map[string]any)
			content, _ := candidate["content"].(map[string]any)
			parts, _ := content["parts"].([]any)
			if len(parts) > 0 {
				part, _ := parts[0].(map[string]any)
				text = fmt.Sprint(part["text"])
			}
			done = candidate["finishReason"] != nil
		}
	}
	if done && text == "" {
		return []byte("[DONE]"), true, nil
	}
	chunk, err := json.Marshal(map[string]any{"id": "chatcmpl-workama", "object": "chat.completion.chunk", "model": callerModel, "choices": []any{map[string]any{"index": 0, "delta": map[string]any{"content": text}, "finish_reason": nil}}})
	return chunk, done, err
}

func valueOr(value any, fallback any) any {
	if value == nil {
		return fallback
	}
	return value
}
func intNumber(value any) int {
	if n, ok := value.(float64); ok {
		return int(n)
	}
	return 0
}
func setModel(raw []byte, model string) []byte {
	var value map[string]any
	if json.Unmarshal(raw, &value) != nil {
		return raw
	}
	value["model"] = model
	encoded, err := json.Marshal(value)
	if err != nil {
		return raw
	}
	return encoded
}
