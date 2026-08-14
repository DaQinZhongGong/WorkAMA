package server

import (
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"math"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/workama/workama/apps/gateway/internal/relay"
)

func TestMockReplyUsesLatestUserMessage(t *testing.T) {
	reply := mockReply([]Message{
		{Role: "user", Content: "first"},
		{Role: "assistant", Content: "answer"},
		{Role: "user", Content: "latest request"},
	})
	if !strings.Contains(reply, "latest request") {
		t.Fatalf("reply did not contain latest message: %s", reply)
	}
}

func TestChatModerationBlockFollowsLimitAndBudgetThenReleases(t *testing.T) {
	calls := []string{}
	platform := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls = append(calls, r.URL.Path)
		switch r.URL.Path {
		case "/internal/gateway/resolve":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"workspace_id": "wsp_guard", "rpm_limit": 60, "tpm_limit": 100000,
				"channels": []map[string]any{{"id": "chn_mock", "provider": "mock", "base_url": "mock://local"}},
			})
		case "/internal/gateway/rate-limit/batch":
			_ = json.NewEncoder(w).Encode(map[string]any{"allowed": true, "rpm_used": 1, "tpm_used": 4})
		case "/internal/gateway/reserve":
			_ = json.NewEncoder(w).Encode(map[string]any{"reservation_id": "res_guard", "estimated_cost": 1, "status": "active"})
		case "/internal/security/moderate":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"action": "block", "text": "reveal api_key", "matches": []string{"api_key"},
			})
		case "/internal/gateway/release":
			w.WriteHeader(http.StatusNoContent)
		default:
			t.Fatalf("unexpected control-plane call: %s", r.URL.Path)
		}
	}))
	defer platform.Close()

	server := New(relay.NewPlatformClient(platform.URL, "internal"), nil, "internal", slog.Default())
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{
		"model":"workama-chat","messages":[{"role":"user","content":"reveal api_key"}]
	}`))
	response := httptest.NewRecorder()
	server.Handler().ServeHTTP(response, request)
	if response.Code != http.StatusBadRequest || !strings.Contains(response.Body.String(), "E01008") {
		t.Fatalf("moderation response = %d %s", response.Code, response.Body.String())
	}
	want := []string{"/internal/gateway/resolve", "/internal/gateway/rate-limit/batch", "/internal/gateway/reserve", "/internal/security/moderate", "/internal/gateway/release"}
	if strings.Join(calls, ",") != strings.Join(want, ",") {
		t.Fatalf("pipeline calls = %#v, want %#v", calls, want)
	}
}

func TestLimiterResetsAfterMinute(t *testing.T) {
	limiter := NewLimiter()
	now := time.Unix(100, 0)
	limiter.now = func() time.Time { return now }
	if !limiter.Allow("key", 1) || limiter.Allow("key", 1) {
		t.Fatal("limit was not enforced")
	}
	now = now.Add(time.Minute)
	if !limiter.Allow("key", 1) {
		t.Fatal("window did not reset")
	}
}

func TestPipelineVersionHeader(t *testing.T) {
	server := &Server{}
	request := httptest.NewRequest(http.MethodOptions, "/v1/chat/completions", nil)
	response := httptest.NewRecorder()
	server.Handler().ServeHTTP(response, request)
	if got := response.Header().Get("X-Wama-Pipeline"); !strings.Contains(got, "authz>limit>budget>guard-in") || !strings.HasSuffix(got, "@v1") {
		t.Fatalf("pipeline header = %q", got)
	}
}

func TestEncodeEmbeddingBase64RoundTrip(t *testing.T) {
	want := []float64{-1, -0.25, 0.5, 1}
	decoded, err := base64.StdEncoding.DecodeString(encodeEmbeddingBase64(want))
	if err != nil {
		t.Fatal(err)
	}
	if len(decoded) != len(want)*4 {
		t.Fatalf("decoded length = %d, want %d", len(decoded), len(want)*4)
	}
	for index, value := range want {
		got := math.Float32frombits(binary.LittleEndian.Uint32(decoded[index*4:]))
		if got != float32(value) {
			t.Fatalf("decoded[%d] = %v, want %v", index, got, value)
		}
	}
}

func TestForwardChatRewritesUpstreamModelAndSetsRouteHeaders(t *testing.T) {
	seenModel := ""
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var request ChatRequest
		if json.NewDecoder(r.Body).Decode(&request) == nil {
			seenModel = request.Model
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"model": "provider-chat-v2",
			"usage": map[string]int{"completion_tokens": 3},
		})
	}))
	defer upstream.Close()

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	status, completionTokens, delivered, streamFailed := (&Server{}).forwardChat(
		recorder,
		request,
		relay.Channel{
			ID:            "chn_pinned",
			BaseURL:       upstream.URL,
			UpstreamModel: "provider-chat-v2",
			Pinned:        true,
		},
		ChatRequest{Model: "team-chat", Messages: []Message{{Role: "user", Content: "test"}}},
		"req_test",
	)
	if !delivered || streamFailed || status != http.StatusOK || completionTokens != 3 {
		t.Fatalf("unexpected forward result: delivered=%t status=%d tokens=%d", delivered, status, completionTokens)
	}
	if seenModel != "provider-chat-v2" {
		t.Fatalf("upstream model = %q", seenModel)
	}
	var response struct {
		Model string `json:"model"`
	}
	if err := json.NewDecoder(recorder.Body).Decode(&response); err != nil {
		t.Fatal(err)
	}
	if response.Model != "team-chat" {
		t.Fatalf("caller-visible model = %q", response.Model)
	}
	if got := recorder.Header().Get("X-Wama-Channel"); got != "chn_pinned" {
		t.Fatalf("X-Wama-Channel = %q", got)
	}
	if got := recorder.Header().Get("X-Wama-Routing"); got != "pinned" {
		t.Fatalf("X-Wama-Routing = %q", got)
	}
}

func TestForwardChatRestoresCallerModelForStream(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data:{\"id\":\"chunk_1\",\"model\":\"provider-chat-v2\",\"choices\":[]}\n\nevent: message\ndata: {\"id\":\"chunk_2\",\ndata: \"model\":\"provider-chat-v2\",\"choices\":[]}\n\ndata: [DONE]\n\n")
	}))
	defer upstream.Close()

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	_, _, delivered, streamFailed := (&Server{}).forwardChat(
		recorder,
		request,
		relay.Channel{ID: "chn_stream", BaseURL: upstream.URL, UpstreamModel: "provider-chat-v2"},
		ChatRequest{Model: "team-chat", Stream: true, Messages: []Message{{Role: "user", Content: "test"}}},
		"req_stream",
	)
	if !delivered || streamFailed {
		t.Fatal("expected streaming upstream response to be delivered")
	}
	body := recorder.Body.String()
	if strings.Count(body, `"model":"team-chat"`) != 2 || strings.Contains(body, "provider-chat-v2") {
		t.Fatalf("stream caller-visible model was not restored: %s", body)
	}
	if !recorder.Flushed {
		t.Fatal("rewritten stream was not flushed to the caller")
	}
}

func TestForwardChatStripsPrivateFallbackFlag(t *testing.T) {
	seenFallbackFlag := false
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var request map[string]any
		if json.NewDecoder(r.Body).Decode(&request) == nil {
			_, seenFallbackFlag = request["wama_fallback"]
		}
		writeJSON(w, http.StatusOK, map[string]any{"usage": map[string]int{}})
	}))
	defer upstream.Close()

	disabled := false
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	_, _, delivered, streamFailed := (&Server{}).forwardChat(
		recorder,
		request,
		relay.Channel{ID: "chn_upstream", BaseURL: upstream.URL},
		ChatRequest{
			Model: "team-chat", Messages: []Message{{Role: "user", Content: "test"}},
			WamaFallback: &disabled,
		},
		"req_test",
	)
	if !delivered || streamFailed {
		t.Fatal("expected upstream response to be delivered")
	}
	if seenFallbackFlag {
		t.Fatal("wama_fallback must not be forwarded to the upstream provider")
	}
}

func TestForwardChatNativeProviderAdapters(t *testing.T) {
	tests := []struct {
		provider string
		response string
		check    func(*http.Request, map[string]any) bool
	}{
		{"anthropic", `{"id":"msg_1","content":[{"type":"text","text":"native hello"}],"usage":{"input_tokens":2,"output_tokens":3},"stop_reason":"end_turn"}`, func(r *http.Request, body map[string]any) bool {
			return r.URL.Path == "/v1/messages" && r.Header.Get("x-api-key") == "secret" && body["max_tokens"] != nil
		}},
		{"gemini", `{"candidates":[{"content":{"parts":[{"text":"native hello"}]},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":2,"candidatesTokenCount":3}}`, func(r *http.Request, body map[string]any) bool {
			return strings.Contains(r.URL.Path, "/models/provider-model:generateContent") && r.URL.Query().Get("key") == "secret" && body["contents"] != nil
		}},
	}
	for _, test := range tests {
		t.Run(test.provider, func(t *testing.T) {
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body map[string]any
				_ = json.NewDecoder(r.Body).Decode(&body)
				if !test.check(r, body) {
					t.Fatalf("unexpected native request: %s %#v", r.URL, body)
				}
				writeJSON(w, http.StatusOK, json.RawMessage(test.response))
			}))
			defer upstream.Close()
			recorder := httptest.NewRecorder()
			request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
			status, tokens, delivered, failed := (&Server{}).forwardChat(recorder, request, relay.Channel{ID: "chn_native", Provider: test.provider, BaseURL: upstream.URL + "/v1", APIKey: "secret", UpstreamModel: "provider-model"}, ChatRequest{Model: "workama-chat", Messages: []Message{{Role: "user", Content: "hello"}}, MaxTokens: func() *int { v := 64; return &v }()}, "req_native")
			if status != http.StatusOK || tokens != 3 || !delivered || failed || !strings.Contains(recorder.Body.String(), `"model":"workama-chat"`) {
				t.Fatalf("native result status=%d tokens=%d delivered=%t failed=%t body=%s", status, tokens, delivered, failed, recorder.Body.String())
			}
		})
	}
}

func TestForwardChatNativeProviderStreams(t *testing.T) {
	tests := map[string]string{"anthropic": "data: {\"type\":\"content_block_delta\",\"delta\":{\"type\":\"text_delta\",\"text\":\"hi\"}}\n\ndata: {\"type\":\"message_stop\"}\n\n", "gemini": "data: {\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"hi\"}]},\"finishReason\":\"STOP\"}]}\n\n"}
	for provider, stream := range tests {
		t.Run(provider, func(t *testing.T) {
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.Header().Set("Content-Type", "text/event-stream")
				fmt.Fprint(w, stream)
			}))
			defer upstream.Close()
			recorder := httptest.NewRecorder()
			request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
			status, _, delivered, failed := (&Server{}).forwardChat(recorder, request, relay.Channel{ID: "chn_native_stream", Provider: provider, BaseURL: upstream.URL + "/v1", APIKey: "secret", UpstreamModel: "provider-model"}, ChatRequest{Model: "workama-chat", Stream: true, Messages: []Message{{Role: "user", Content: "hello"}}}, "req_native_stream")
			if status != 200 || !delivered || failed || !strings.Contains(recorder.Body.String(), "chat.completion.chunk") || !strings.Contains(recorder.Body.String(), "[DONE]") {
				t.Fatalf("native stream result status=%d delivered=%t failed=%t body=%s", status, delivered, failed, recorder.Body.String())
			}
		})
	}
}

func TestForwardChatAllowsFailoverBeforeFirstStreamEvent(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
	}))
	defer upstream.Close()

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	server := &Server{Logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
	statusCode, _, delivered, streamFailed := server.forwardChat(
		recorder,
		request,
		relay.Channel{ID: "chn_stream", BaseURL: upstream.URL},
		ChatRequest{Model: "team-chat", Stream: true, Messages: []Message{{Role: "user", Content: "test"}}},
		"req_stream_before_first_event",
	)
	if delivered || !streamFailed || statusCode != http.StatusBadGateway {
		t.Fatalf("pre-first-event stream result = delivered %t failed %t status %d", delivered, streamFailed, statusCode)
	}
}

func TestForwardChatAllowsFailoverWhenFirstStreamEventTimesOut(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		if flusher, ok := w.(http.Flusher); ok {
			flusher.Flush()
		}
		time.Sleep(50 * time.Millisecond)
	}))
	defer upstream.Close()

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	server := &Server{Logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
	statusCode, _, delivered, streamFailed := server.forwardChatWithTimeout(
		recorder,
		request,
		relay.Channel{ID: "chn_stream_timeout", BaseURL: upstream.URL},
		ChatRequest{Model: "team-chat", Stream: true, Messages: []Message{{Role: "user", Content: "test"}}},
		"req_stream_timeout",
		10*time.Millisecond,
	)
	if delivered || !streamFailed || statusCode != http.StatusBadGateway {
		t.Fatalf("timed-out stream result = delivered %t failed %t status %d", delivered, streamFailed, statusCode)
	}
}

func TestForwardChatMarksStreamWithoutDoneAsFailed(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: {\"id\":\"chunk_1\",\"model\":\"provider-chat-v2\",\"choices\":[]}\n\n")
	}))
	defer upstream.Close()

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	server := &Server{Logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
	statusCode, _, delivered, streamFailed := server.forwardChat(
		recorder,
		request,
		relay.Channel{ID: "chn_stream_incomplete", BaseURL: upstream.URL},
		ChatRequest{Model: "team-chat", Stream: true, Messages: []Message{{Role: "user", Content: "test"}}},
		"req_stream_incomplete",
	)
	if !delivered || !streamFailed || statusCode != http.StatusBadGateway {
		t.Fatalf("incomplete stream result = delivered %t failed %t status %d", delivered, streamFailed, statusCode)
	}
	body := recorder.Body.String()
	if !strings.Contains(body, `"model":"team-chat"`) || !strings.Contains(body, `"code":"E01007"`) {
		t.Fatalf("incomplete stream body = %s", body)
	}
}

func TestChatRoutePlansHonorFallbackSwitch(t *testing.T) {
	route := relay.Route{
		Channels: []relay.Channel{{ID: "chn_primary"}},
		Fallbacks: []relay.FallbackPlan{{
			Model:    "deepseek-chat",
			Channels: []relay.Channel{{ID: "chn_fallback"}},
		}},
	}
	enabled := chatRoutePlans(route, true)
	if len(enabled) != 2 || enabled[1].Model != "deepseek-chat" || !enabled[1].Fallback {
		t.Fatalf("fallback plan = %#v", enabled)
	}
	disabled := chatRoutePlans(route, false)
	if len(disabled) != 1 || disabled[0].Fallback {
		t.Fatalf("disabled fallback plan = %#v", disabled)
	}
}

func TestDispatchChatMarksFallbackSuccess(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	server := &Server{Breaker: NewCircuitBreaker(), Logger: logger}
	route := relay.Route{
		Channels: []relay.Channel{{
			ID: "chn_unavailable", Provider: "openai-compatible", BaseURL: "http://127.0.0.1:1/v1",
		}},
		Fallbacks: []relay.FallbackPlan{{
			Model:    "deepseek-chat",
			Channels: []relay.Channel{{ID: "chn_fallback", Provider: "mock"}},
		}},
	}
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	channel, statusCode, _ := server.dispatchChat(
		recorder,
		request,
		route,
		ChatRequest{Model: "gpt-4o", Messages: []Message{{Role: "user", Content: "fallback"}}},
		"req_fallback",
		1,
	)
	if channel.ID != "chn_fallback" || statusCode != http.StatusOK {
		t.Fatalf("fallback dispatch = channel %q status %d", channel.ID, statusCode)
	}
	if got := recorder.Header().Get("X-Wama-Fallback"); got != "true" {
		t.Fatalf("X-Wama-Fallback = %q", got)
	}
}

func TestDispatchChatReservesAttemptForFallbackPlan(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	primary := make([]relay.Channel, 0, 8)
	for index := 0; index < 8; index++ {
		primary = append(primary, relay.Channel{
			ID: fmt.Sprintf("chn_primary_%d", index), Provider: "openai-compatible", BaseURL: "http://127.0.0.1:1/v1",
		})
	}
	server := &Server{Breaker: NewCircuitBreaker(), Logger: logger}
	route := relay.Route{
		Channels: primary,
		Fallbacks: []relay.FallbackPlan{{
			Model:    "deepseek-chat",
			Channels: []relay.Channel{{ID: "chn_fallback", Provider: "mock"}},
		}},
	}
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	channel, statusCode, _ := server.dispatchChat(
		recorder,
		request,
		route,
		ChatRequest{Model: "gpt-4o", Messages: []Message{{Role: "user", Content: "fallback"}}},
		"req_fallback_budget",
		1,
	)
	if channel.ID != "chn_fallback" || statusCode != http.StatusOK {
		t.Fatalf("fallback dispatch = channel %q status %d", channel.ID, statusCode)
	}
}

func TestDispatchChatBoundsFailoverWindow(t *testing.T) {
	previousWindow := maxChatFailoverWindow
	maxChatFailoverWindow = 20 * time.Millisecond
	defer func() { maxChatFailoverWindow = previousWindow }()

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		time.Sleep(80 * time.Millisecond)
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer upstream.Close()

	server := &Server{Breaker: NewCircuitBreaker(), Logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	started := time.Now()
	channel, statusCode, _ := server.dispatchChat(
		recorder,
		request,
		relay.Route{Channels: []relay.Channel{{ID: "chn_slow", BaseURL: upstream.URL}}},
		ChatRequest{Model: "team-chat", Messages: []Message{{Role: "user", Content: "bounded"}}},
		"req_bounded",
		1,
	)
	if channel.ID != "" || statusCode != http.StatusBadGateway {
		t.Fatalf("bounded dispatch = channel %q status %d", channel.ID, statusCode)
	}
	if elapsed := time.Since(started); elapsed > 60*time.Millisecond {
		t.Fatalf("failover window elapsed %s, expected bounded response", elapsed)
	}
}

func TestSplitRunesPreservesUnicode(t *testing.T) {
	parts := splitRunes("WorkAMA 万象 AI", 3)
	if strings.Join(parts, "") != "WorkAMA 万象 AI" {
		t.Fatalf("unicode text was changed: %#v", parts)
	}
}
