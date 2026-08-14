package server

import (
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/workama/workama/apps/gateway/internal/adapter"
	"github.com/workama/workama/apps/gateway/internal/relay"
)

func TestDispatchChatPrefersStagingChannelWhenConfigured(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/chat/completions" {
			t.Fatalf("unexpected upstream path: %s", r.URL.Path)
		}
		if auth := r.Header.Get("Authorization"); auth != "Bearer sk-test" {
			t.Fatalf("unexpected upstream auth: %q", auth)
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"id":     "chatcmpl-staging",
			"object": "chat.completion",
			"model":  "gpt-4o-mini",
			"choices": []map[string]any{{
				"index": 0,
				"message": map[string]any{
					"role":    "assistant",
					"content": "staging reply",
				},
				"finish_reason": "stop",
			}},
			"usage": map[string]int{"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
		})
	}))
	defer upstream.Close()

	server := &Server{
		Breaker: NewCircuitBreaker(),
		Logger:  slog.New(slog.NewTextHandler(io.Discard, nil)),
		staging: &adapter.StagingConfig{
			Provider: "openai",
			APIKey:   "sk-test",
			BaseURL:  upstream.URL,
			Model:    "gpt-4o-mini",
			Enabled:  true,
		},
	}

	route := relay.Route{Channels: []relay.Channel{{ID: "chn_mock", Provider: "mock"}}}
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	channel, statusCode, completionTokens := server.dispatchChat(
		recorder,
		request,
		route,
		ChatRequest{Model: "workama-chat", Messages: []Message{{Role: "user", Content: "hello"}}},
		"req_staging",
		1,
	)

	if channel.ID != "staging" || statusCode != http.StatusOK {
		t.Fatalf("expected staging channel ok, got %q status %d", channel.ID, statusCode)
	}
	if completionTokens != 2 {
		t.Fatalf("completion tokens = %d", completionTokens)
	}
	body := recorder.Body.String()
	if !strings.Contains(body, `"model":"workama-chat"`) || !strings.Contains(body, "staging reply") {
		t.Fatalf("unexpected body: %s", body)
	}
	if got := recorder.Header().Get("X-Wama-Channel"); got != "staging" {
		t.Fatalf("X-Wama-Channel = %q", got)
	}
}

func TestDispatchChatFallsBackToMockWhenStagingNotConfigured(t *testing.T) {
	server := &Server{
		Breaker: NewCircuitBreaker(),
		Logger:  slog.New(slog.NewTextHandler(io.Discard, nil)),
	}

	route := relay.Route{Channels: []relay.Channel{{ID: "chn_mock", Provider: "mock"}}}
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	channel, statusCode, _ := server.dispatchChat(
		recorder,
		request,
		route,
		ChatRequest{Model: "workama-chat", Messages: []Message{{Role: "user", Content: "hello"}}},
		"req_mock",
		1,
	)

	if channel.ID != "chn_mock" || statusCode != http.StatusOK {
		t.Fatalf("expected mock channel ok, got %q status %d", channel.ID, statusCode)
	}
	if !strings.Contains(recorder.Body.String(), "local verification model") {
		t.Fatalf("expected mock reply, got %s", recorder.Body.String())
	}
}

func TestDispatchChatStagingStream(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: {\"id\":\"c1\",\"model\":\"upstream\",\"choices\":[{\"delta\":{\"content\":\"hi\"}}]}\n\ndata: [DONE]\n\n")
	}))
	defer upstream.Close()

	server := &Server{
		Breaker: NewCircuitBreaker(),
		Logger:  slog.New(slog.NewTextHandler(io.Discard, nil)),
		staging: &adapter.StagingConfig{
			Provider: "openai",
			APIKey:   "sk-test",
			BaseURL:  upstream.URL,
			Model:    "gpt-4o-mini",
			Enabled:  true,
		},
	}

	route := relay.Route{Channels: []relay.Channel{{ID: "chn_mock", Provider: "mock"}}}
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	channel, statusCode, _ := server.dispatchChat(
		recorder,
		request,
		route,
		ChatRequest{Model: "workama-chat", Stream: true, Messages: []Message{{Role: "user", Content: "hello"}}},
		"req_staging_stream",
		1,
	)

	if channel.ID != "staging" || statusCode != http.StatusOK {
		t.Fatalf("expected staging stream ok, got %q status %d", channel.ID, statusCode)
	}
	body := recorder.Body.String()
	if !strings.Contains(body, `"delta"`) || !strings.Contains(body, "[DONE]") || !strings.Contains(body, `"model":"workama-chat"`) {
		t.Fatalf("unexpected stream body: %s", body)
	}
}
