package adapter

import (
	"context"
	"encoding/json"
	"io"
	"strings"
	"testing"
)

func TestFirstEightProviderContract(t *testing.T) {
	for _, provider := range Providers() {
		t.Run(provider, func(t *testing.T) {
			a, err := Resolve(provider)
			if err != nil {
				t.Fatal(err)
			}
			profile := a.DescribeCapabilities(context.Background())
			if profile.Provider != provider || len(profile.Capabilities) == 0 || len(profile.Regions) == 0 || profile.RetentionMode == "" {
				t.Fatalf("incomplete profile: %#v", profile)
			}
			req, err := a.BuildChatRequest(context.Background(), Channel{Provider: provider, BaseURL: "https://provider.example/v1", APIKey: "secret-value", Model: "provider-model"}, map[string]any{"model": "workama-chat", "messages": []any{map[string]any{"role": "user", "content": "hello"}}, "stream": false})
			if err != nil {
				t.Fatal(err)
			}
			body, _ := io.ReadAll(req.Body)
			if strings.Contains(string(body), "secret-value") {
				t.Fatal("credential leaked into request body")
			}
			var raw []byte
			switch profile.NativeProtocol {
			case "anthropic":
				raw = []byte(`{"id":"msg_1","content":[{"type":"text","text":"hello"}],"usage":{"input_tokens":2,"output_tokens":1},"stop_reason":"end_turn"}`)
			case "gemini":
				raw = []byte(`{"candidates":[{"content":{"parts":[{"text":"hello"}]},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":2,"candidatesTokenCount":1}}`)
			default:
				raw = []byte(`{"id":"chat_1","model":"provider-model","choices":[{"message":{"role":"assistant","content":"hello"}}],"usage":{"prompt_tokens":2,"completion_tokens":1}}`)
			}
			parsed, err := a.ParseChatResponse(raw, "workama-chat")
			if err != nil {
				t.Fatal(err)
			}
			var response map[string]any
			if json.Unmarshal(parsed, &response) != nil || response["model"] != "workama-chat" {
				t.Fatalf("invalid unified response: %s", parsed)
			}
		})
	}
}

func TestNativeStreamChunksBecomeOpenAIChunks(t *testing.T) {
	cases := map[string][]byte{"anthropic": []byte(`{"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}`), "gemini": []byte(`{"candidates":[{"content":{"parts":[{"text":"hi"}]}}]}`)}
	for provider, raw := range cases {
		a, _ := Resolve(provider)
		parsed, done, err := a.ParseStreamChunk(raw, "workama-chat")
		if err != nil || done || !strings.Contains(string(parsed), `"object":"chat.completion.chunk"`) {
			t.Fatalf("%s stream=%s done=%t err=%v", provider, parsed, done, err)
		}
	}
}
