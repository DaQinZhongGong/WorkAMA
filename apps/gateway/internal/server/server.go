package server

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math"
	"net/http"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
	"unicode/utf8"

	"github.com/workama/workama/apps/gateway/internal/adapter"
	"github.com/workama/workama/apps/gateway/internal/metering"
	"github.com/workama/workama/apps/gateway/internal/observability"
	"github.com/workama/workama/apps/gateway/internal/relay"
	"github.com/workama/workama/apps/gateway/internal/token"
	commonobservability "github.com/workama/workama/packages/go-common/observability"
)

// HealthChecker checks the health of downstream dependencies (e.g. database).
type HealthChecker interface {
	CheckHealth(ctx context.Context) error
}

type Server struct {
	Platform      *relay.PlatformClient
	Metering      *metering.Client
	Breaker       *CircuitBreaker
	InternalToken string
	Logger        *slog.Logger
	responses     responseRegistry
	staging       *adapter.StagingConfig
	// ChatHandlerOverride 在非空时替换默认的 /v1/chat/completions 处理器。
	// 由 main.go 注入 pg 直连版 10 步管道处理器。
	ChatHandlerOverride http.Handler
	// ModelsHandlerOverride 在非空时替换默认的 /v1/models 处理器。
	ModelsHandlerOverride http.Handler
	// HealthChecker 用于 /healthz 检查下游依赖健康状态。
	HealthChecker HealthChecker
}

type Message struct {
	Role       string `json:"role"`
	Content    any    `json:"content"`
	ToolCallID string `json:"tool_call_id,omitempty"`
	Name       string `json:"name,omitempty"`
	ToolCalls  any    `json:"tool_calls,omitempty"`
}

type ChatRequest struct {
	Model        string    `json:"model"`
	Messages     []Message `json:"messages"`
	Stream       bool      `json:"stream"`
	Temperature  *float64  `json:"temperature,omitempty"`
	TopP         *float64  `json:"top_p,omitempty"`
	MaxTokens    *int      `json:"max_tokens,omitempty"`
	Tools        []any     `json:"tools,omitempty"`
	ToolChoice   any       `json:"tool_choice,omitempty"`
	WamaFallback *bool     `json:"wama_fallback,omitempty"`
}

type chatRoutePlan struct {
	Model    string
	Channels []relay.Channel
	Fallback bool
}

type sseEventState struct {
	hasData bool
	payload bool
	done    bool
}

type moderationWorkspaceKey struct{}

var (
	maxChatFailoverWindow       = 45 * time.Second
	upstreamFirstResponseWindow = 15 * time.Second
	errModerationBlocked        = errors.New("moderation blocked output")
)

type EmbeddingRequest struct {
	Model          string `json:"model"`
	Input          any    `json:"input"`
	EncodingFormat string `json:"encoding_format,omitempty"`
}

func New(platform *relay.PlatformClient, meter *metering.Client, internalToken string, logger *slog.Logger) *Server {
	return NewWithResponsePersistence(
		platform,
		meter,
		internalToken,
		logger,
		newResponsePersistenceFromEnv(logger),
		responseRegistryTTLFromEnv(),
	)
}

func NewWithResponsePersistence(platform *relay.PlatformClient, meter *metering.Client, internalToken string, logger *slog.Logger, persistence responsePersistence, responseTTL time.Duration) *Server {
	service := &Server{
		Platform: platform, Metering: meter, Breaker: NewCircuitBreaker(),
		InternalToken: internalToken, Logger: logger,
	}
	service.responses = newResponseRegistry(persistence, responseTTL, logger)
	service.staging = adapter.LoadStagingConfig()
	return service
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.health)
	if s.ModelsHandlerOverride != nil {
		mux.Handle("GET /v1/models", s.ModelsHandlerOverride)
	} else {
		mux.HandleFunc("GET /v1/models", s.models)
	}
	if s.ChatHandlerOverride != nil {
		mux.Handle("POST /v1/chat/completions", s.ChatHandlerOverride)
	} else {
		mux.HandleFunc("POST /v1/chat/completions", s.chatCompletions)
	}
	mux.HandleFunc("POST /v1/embeddings", s.embeddings)
	mux.HandleFunc("POST /v1/images/generations", s.imagesGenerations)
	mux.HandleFunc("POST /v1/images/edits", s.imagesEdits)
	mux.HandleFunc("POST /v1/audio/speech", s.audioSpeech)
	mux.HandleFunc("POST /v1/audio/transcriptions", s.audioTranscriptions)
	mux.HandleFunc("POST /v1/responses", s.createResponse)
	mux.HandleFunc("GET /v1/responses/{response_id}", s.getResponse)
	mux.HandleFunc("POST /v1/responses/{response_id}/cancel", s.cancelResponse)
	s.recoverBackgroundResponses()
	return observability.Middleware("gateway", s.withRequestHeaders(mux))
}

func (s *Server) withRequestHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Wama-Pipeline", "authz>limit>budget>guard-in>map>route>forward>guard-out>meter@v1")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	if s.HealthChecker != nil {
		if err := s.HealthChecker.CheckHealth(r.Context()); err != nil {
			s.Logger.Error("health check failed", "error", err)
			writeJSON(w, http.StatusServiceUnavailable, map[string]string{"status": "unavailable", "service": "gateway", "reason": "dependency check failed"})
			return
		}
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok", "service": "gateway"})
}

func (s *Server) authenticate(r *http.Request, model string) (relay.Route, error) {
	auth := r.Header.Get("Authorization")
	apiKey := ""
	if strings.HasPrefix(auth, "Bearer ") {
		apiKey = strings.TrimSpace(strings.TrimPrefix(auth, "Bearer "))
	}
	workspaceID := ""
	if apiKey == "" && token.EqualSecret(r.Header.Get("X-Internal-Token"), s.InternalToken) {
		workspaceID = r.Header.Get("X-Workspace-ID")
	}
	return s.Platform.Resolve(r.Context(), apiKey, workspaceID, model)
}

func (s *Server) models(w http.ResponseWriter, r *http.Request) {
	route, err := s.authenticate(r, "workama-chat")
	if err != nil {
		s.writeResolveError(w, err)
		return
	}
	channel := firstChannel(route)
	writeJSON(w, http.StatusOK, map[string]any{
		"object": "list",
		"data": []map[string]any{
			{"id": "workama-chat", "object": "model", "created": time.Now().Unix(), "owned_by": "workama"},
			{"id": "workama-embed", "object": "model", "created": time.Now().Unix(), "owned_by": "workama"},
		},
		"x_workama_channel": channel.Provider,
	})
}

func (s *Server) chatCompletions(w http.ResponseWriter, r *http.Request) {
	started := time.Now()
	requestID := requestID(r)
	var body ChatRequest
	if err := decodeJSON(r.Body, &body); err != nil {
		writeOpenAIError(w, http.StatusBadRequest, "E00001", "Invalid JSON request")
		return
	}
	if body.Model == "" || len(body.Messages) == 0 {
		writeOpenAIError(w, http.StatusBadRequest, "E00001", "model and messages are required")
		return
	}
	route, err := s.authenticate(r, body.Model)
	if err != nil {
		s.writeResolveError(w, err)
		return
	}
	promptTokens := estimateMessageTokens(body.Messages)
	if !s.enforceRateLimit(w, r, route, promptTokens) {
		return
	}
	estimatedTokens := promptTokens + 1024
	if body.MaxTokens != nil && *body.MaxTokens > 0 {
		estimatedTokens = promptTokens + *body.MaxTokens
	}
	if _, err := s.Platform.Reserve(r.Context(), requestID, route.WorkspaceID, body.Model, estimatedTokens); err != nil {
		s.writeBudgetError(w, err)
		return
	}
	if !s.moderateMessages(w, r, route.WorkspaceID, &body, requestID) {
		if err := s.Platform.Release(context.WithoutCancel(r.Context()), requestID); err != nil {
			s.Logger.Error("budget release after input guard failed", "request_id", requestID, "error", err)
		}
		return
	}
	r = r.WithContext(context.WithValue(r.Context(), moderationWorkspaceKey{}, route.WorkspaceID))
	channel, statusCode, completionTokens := s.dispatchChat(w, r, route, body, requestID, promptTokens)
	if channel.ID == "" {
		if err := s.Platform.Release(context.WithoutCancel(r.Context()), requestID); err != nil {
			s.Logger.Error("budget release failed", "request_id", requestID, "error", err)
		}
		return
	}
	s.recordMeter(r.Context(), metering.Record{
		RequestID: requestID, WorkspaceID: route.WorkspaceID, TokenID: route.TokenID,
		ChannelID: channel.ID, Model: body.Model, PromptTokens: promptTokens,
		CompletionTokens: completionTokens, LatencyMS: time.Since(started).Milliseconds(), StatusCode: statusCode,
	})
}

func (s *Server) dispatchChat(w http.ResponseWriter, r *http.Request, route relay.Route, body ChatRequest, requestID string, promptTokens int) (relay.Channel, int, int) {
	attempted := false
	fallbackEnabled := body.WamaFallback == nil || *body.WamaFallback
	failoverDeadline := time.Now().Add(maxChatFailoverWindow)
	plans := chatRoutePlans(route, fallbackEnabled)
	if s.staging != nil && s.staging.Enabled {
		plans = append([]chatRoutePlan{{
			Channels: []relay.Channel{{
				ID: "staging", Provider: "staging", BaseURL: s.staging.BaseURL,
				APIKey: s.staging.APIKey, UpstreamModel: s.staging.Model,
			}},
		}}, plans...)
	}
	for _, plan := range plans {
		planAttempts := 0
		for _, channel := range plan.Channels {
			if planAttempts >= 3 {
				break
			}
			remaining := time.Until(failoverDeadline)
			if remaining <= 0 {
				break
			}
			if !s.Breaker.Allow(channel.ID) {
				continue
			}
			attempted = true
			planAttempts++
			channel.Fallback = plan.Fallback
			if channel.Provider == "mock" {
				setRouteHeaders(w, channel)
				if toolCall := mockToolRequest(body); toolCall != nil {
					if body.Stream {
						s.streamMockToolCall(w, r, requestID, body.Model, toolCall)
					} else {
						writeJSON(w, http.StatusOK, map[string]any{"id": requestID, "object": "chat.completion", "model": body.Model, "choices": []map[string]any{{"index": 0, "message": map[string]any{"role": "assistant", "content": nil, "tool_calls": []any{toolCall}}, "finish_reason": "tool_calls"}}, "usage": map[string]int{"prompt_tokens": promptTokens, "completion_tokens": 1, "total_tokens": promptTokens + 1}})
					}
					s.Breaker.Record(channel.ID, false)
					return channel, http.StatusOK, 1
				}
				reply := mockReply(body.Messages)
				if s.Platform == nil || route.WorkspaceID == "" {
					completionTokens := estimateTokens(reply)
					if body.Stream {
						s.streamMock(w, r, requestID, body.Model, reply)
					} else {
						writeJSON(w, http.StatusOK, map[string]any{
							"id": requestID, "object": "chat.completion", "created": time.Now().Unix(), "model": body.Model,
							"choices": []map[string]any{{"index": 0, "message": map[string]string{"role": "assistant", "content": reply}, "finish_reason": "stop"}},
							"usage":   map[string]int{"prompt_tokens": promptTokens, "completion_tokens": completionTokens, "total_tokens": promptTokens + completionTokens},
						})
					}
					s.Breaker.Record(channel.ID, false)
					return channel, http.StatusOK, completionTokens
				}
				decision, err := s.Platform.Moderate(r.Context(), route.WorkspaceID, "output", reply, requestID)
				if err != nil {
					s.Logger.Error("output moderation failed", "request_id", requestID, "error", err)
					writeOpenAIError(w, http.StatusServiceUnavailable, "E01007", "Content safety service is unavailable")
					return relay.Channel{}, http.StatusServiceUnavailable, 0
				}
				if decision.Action == "block" {
					writeOpenAIError(w, http.StatusBadRequest, "E01009", "Model output was blocked by workspace policy")
					return relay.Channel{}, http.StatusBadRequest, 0
				}
				if decision.Action == "mask" {
					reply = decision.Text
				}
				completionTokens := estimateTokens(reply)
				if body.Stream {
					s.streamMock(w, r, requestID, body.Model, reply)
				} else {
					writeJSON(w, http.StatusOK, map[string]any{
						"id": requestID, "object": "chat.completion", "created": time.Now().Unix(), "model": body.Model,
						"choices": []map[string]any{{"index": 0, "message": map[string]string{"role": "assistant", "content": reply}, "finish_reason": "stop"}},
						"usage":   map[string]int{"prompt_tokens": promptTokens, "completion_tokens": completionTokens, "total_tokens": promptTokens + completionTokens},
					})
				}
				s.Breaker.Record(channel.ID, false)
				return channel, http.StatusOK, completionTokens
			}
			attemptTimeout := minDuration(upstreamFirstResponseWindow, remaining)
			statusCode, completionTokens, delivered, streamFailed := s.forwardChatWithTimeout(w, r, channel, body, requestID, attemptTimeout)
			if delivered {
				s.Breaker.Record(channel.ID, streamFailed)
				return channel, statusCode, completionTokens
			}
			s.Breaker.Record(channel.ID, true)
			s.Logger.Warn("gateway channel failed, trying next candidate", "request_id", requestID, "channel_id", channel.ID, "status", statusCode, "fallback", plan.Fallback)
		}
	}
	if !attempted {
		writeOpenAIError(w, http.StatusServiceUnavailable, "E02003", "All matching channels are temporarily circuit-broken")
		return relay.Channel{}, http.StatusServiceUnavailable, 0
	}
	writeOpenAIError(w, http.StatusBadGateway, "E01007", "All upstream channels failed")
	return relay.Channel{}, http.StatusBadGateway, 0
}

func (s *Server) moderateMessages(w http.ResponseWriter, r *http.Request, workspaceID string, body *ChatRequest, requestID string) bool {
	for index := range body.Messages {
		content, ok := body.Messages[index].Content.(string)
		if !ok || content == "" {
			continue
		}
		decision, err := s.Platform.Moderate(r.Context(), workspaceID, "input", content, requestID)
		if err != nil {
			s.Logger.Error("input moderation failed", "request_id", requestID, "error", err)
			writeOpenAIError(w, http.StatusServiceUnavailable, "E01007", "Content safety service is unavailable")
			return false
		}
		if decision.Action == "block" {
			writeOpenAIError(w, http.StatusBadRequest, "E01008", "Input was blocked by workspace policy")
			return false
		}
		if decision.Action == "mask" {
			body.Messages[index].Content = decision.Text
		}
	}
	return true
}

func (s *Server) streamMock(w http.ResponseWriter, r *http.Request, id, model, reply string) {
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeOpenAIError(w, http.StatusInternalServerError, "E01007", "Streaming is unavailable")
		return
	}
	writeSSE(w, map[string]any{
		"id": id, "object": "chat.completion.chunk", "created": time.Now().Unix(), "model": model,
		"choices": []map[string]any{{"index": 0, "delta": map[string]string{"role": "assistant"}, "finish_reason": nil}},
	})
	flusher.Flush()
	for _, chunk := range splitRunes(reply, 12) {
		select {
		case <-r.Context().Done():
			return
		default:
		}
		writeSSE(w, map[string]any{
			"id": id, "object": "chat.completion.chunk", "created": time.Now().Unix(), "model": model,
			"choices": []map[string]any{{"index": 0, "delta": map[string]string{"content": chunk}, "finish_reason": nil}},
		})
		flusher.Flush()
		time.Sleep(12 * time.Millisecond)
	}
	writeSSE(w, map[string]any{
		"id": id, "object": "chat.completion.chunk", "created": time.Now().Unix(), "model": model,
		"choices": []map[string]any{{"index": 0, "delta": map[string]string{}, "finish_reason": "stop"}},
	})
	fmt.Fprint(w, "data: [DONE]\n\n")
	flusher.Flush()
}

func mockToolRequest(body ChatRequest) map[string]any {
	if len(body.Tools) == 0 {
		return nil
	}
	requested := false
	for _, message := range body.Messages {
		if message.Role == "user" {
			if content, ok := message.Content.(string); ok && strings.HasPrefix(strings.TrimSpace(content), "/native-tool") {
				requested = true
			}
		}
		if requested && message.Role == "tool" {
			return nil
		}
	}
	if !requested {
		return nil
	}
	return map[string]any{"index": 0, "id": "call_mock_native", "type": "function", "function": map[string]any{"name": "file.write", "arguments": "{\"path\":\"native/tool.txt\",\"content\":\"native-tool-ok\"}"}}
}

func (s *Server) streamMockToolCall(w http.ResponseWriter, r *http.Request, id, model string, call map[string]any) {
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Connection", "keep-alive")
	flusher, ok := w.(http.Flusher)
	if !ok {
		return
	}
	writeSSE(w, map[string]any{"id": id, "object": "chat.completion.chunk", "model": model, "choices": []map[string]any{{"index": 0, "delta": map[string]any{"role": "assistant", "tool_calls": []any{call}}, "finish_reason": nil}}})
	flusher.Flush()
	writeSSE(w, map[string]any{"id": id, "object": "chat.completion.chunk", "model": model, "choices": []map[string]any{{"index": 0, "delta": map[string]any{}, "finish_reason": "tool_calls"}}})
	fmt.Fprint(w, "data: [DONE]\n\n")
	flusher.Flush()
}

func (s *Server) forwardChat(w http.ResponseWriter, r *http.Request, channel relay.Channel, body ChatRequest, requestID string) (int, int, bool, bool) {
	return s.forwardChatWithTimeout(w, r, channel, body, requestID, upstreamFirstResponseWindow)
}

func (s *Server) forwardChatWithTimeout(w http.ResponseWriter, r *http.Request, channel relay.Channel, body ChatRequest, requestID string, attemptTimeout time.Duration) (int, int, bool, bool) {
	if attemptTimeout <= 0 {
		return http.StatusBadGateway, 0, false, true
	}
	upstreamBody := body
	upstreamBody.WamaFallback = nil
	if channel.UpstreamModel != "" {
		upstreamBody.Model = channel.UpstreamModel
	}
	payload, _ := json.Marshal(upstreamBody)
	var unified map[string]any
	if err := json.Unmarshal(payload, &unified); err != nil {
		return http.StatusBadGateway, 0, false, true
	}
	providerAdapter, err := adapter.Resolve(channel.Provider)
	if err != nil {
		return http.StatusBadGateway, 0, false, true
	}
	requestContext := r.Context()
	cancel := func() {}
	if !body.Stream {
		requestContext, cancel = context.WithTimeout(requestContext, attemptTimeout)
	}
	defer cancel()
	req, err := providerAdapter.BuildChatRequest(requestContext, adapter.Channel{Provider: channel.Provider, BaseURL: channel.BaseURL, APIKey: channel.APIKey, Model: upstreamBody.Model}, unified)
	if err != nil {
		return http.StatusBadGateway, 0, false, true
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.ResponseHeaderTimeout = attemptTimeout
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: commonobservability.Transport(transport)}
	resp, err := client.Do(req)
	if err != nil {
		return http.StatusBadGateway, 0, false, true
	}
	defer resp.Body.Close()
	if shouldFailover(resp.StatusCode) {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10))
		return resp.StatusCode, 0, false, true
	}
	writeHeaders := func() {
		for key, values := range resp.Header {
			if strings.EqualFold(key, "Content-Length") || strings.EqualFold(key, "Transfer-Encoding") {
				continue
			}
			for _, value := range values {
				w.Header().Add(key, value)
			}
		}
		setRouteHeaders(w, channel)
		w.Header().Set("X-Request-ID", requestID)
		w.WriteHeader(resp.StatusCode)
	}
	if body.Stream {
		workspaceID, _ := r.Context().Value(moderationWorkspaceKey{}).(string)
		if normalized := adapter.NormalizeProvider(channel.Provider); normalized == "anthropic" || normalized == "gemini" {
			started, streamErr := s.copyNativeAdapterStream(w, resp.Body, providerAdapter, body.Model, attemptTimeout, writeHeaders, r.Context(), workspaceID, requestID)
			if !started {
				return http.StatusBadGateway, 0, false, true
			}
			if streamErr != nil {
				return http.StatusBadGateway, 0, true, true
			}
			return resp.StatusCode, 0, true, false
		}
		transform := func(lines []string) ([]string, error) { return lines, nil }
		if workspaceID != "" {
			transform = func(lines []string) ([]string, error) {
				return s.moderateSSEEvent(r.Context(), workspaceID, lines, requestID)
			}
		}
		started, streamErr := copyChatStreamTransformed(w, resp.Body, body.Model, attemptTimeout, writeHeaders, transform)
		if errors.Is(streamErr, errModerationBlocked) {
			if !started {
				writeOpenAIError(w, http.StatusBadRequest, "E01009", "Model output was blocked by workspace policy")
			} else {
				writeSSE(w, map[string]any{"error": map[string]string{
					"message": "Model output was blocked by workspace policy", "type": "workama_error", "code": "E01009",
				}})
				if flusher, ok := w.(http.Flusher); ok {
					flusher.Flush()
				}
			}
			return http.StatusBadRequest, 0, true, false
		}
		if !started {
			s.Logger.Warn("upstream stream ended before the first event", "request_id", requestID, "channel_id", channel.ID, "error", streamErr)
			return http.StatusBadGateway, 0, false, true
		}
		if streamErr != nil {
			s.Logger.Warn("upstream stream interrupted after delivery", "request_id", requestID, "channel_id", channel.ID, "error", streamErr)
			writeSSE(w, map[string]any{"error": map[string]string{
				"message": "Upstream stream interrupted", "type": "api_error", "code": "E01007",
			}})
			if flusher, ok := w.(http.Flusher); ok {
				flusher.Flush()
			}
			return http.StatusBadGateway, 0, true, true
		}
		return resp.StatusCode, 0, true, false
	}
	responseBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return http.StatusBadGateway, 0, false, true
	}
	responseBody, err = providerAdapter.ParseChatResponse(responseBody, body.Model)
	if err != nil {
		return http.StatusBadGateway, 0, false, true
	}
	if workspaceID, ok := r.Context().Value(moderationWorkspaceKey{}).(string); ok && workspaceID != "" {
		moderated, decision, moderationErr := s.moderateJSONResponse(r.Context(), workspaceID, responseBody, requestID)
		if moderationErr != nil {
			s.Logger.Error("upstream output moderation failed", "request_id", requestID, "error", moderationErr)
			writeOpenAIError(w, http.StatusServiceUnavailable, "E01007", "Content safety service is unavailable")
			return http.StatusServiceUnavailable, 0, true, true
		}
		if decision == "block" {
			writeOpenAIError(w, http.StatusBadRequest, "E01009", "Model output was blocked by workspace policy")
			return http.StatusBadRequest, 0, true, false
		}
		responseBody = moderated
	}
	writeHeaders()
	_, _ = w.Write(responseBody)
	var decoded struct {
		Usage struct {
			CompletionTokens int `json:"completion_tokens"`
		} `json:"usage"`
	}
	_ = json.Unmarshal(responseBody, &decoded)
	return resp.StatusCode, decoded.Usage.CompletionTokens, true, false
}

func (s *Server) copyNativeAdapterStream(destination http.ResponseWriter, source io.ReadCloser, providerAdapter adapter.Adapter, model string, firstEventTimeout time.Duration, start func(), ctx context.Context, workspaceID, requestID string) (bool, error) {
	scanner := bufio.NewScanner(source)
	scanner.Buffer(make([]byte, 0, 64<<10), 1<<20)
	flusher, _ := destination.(http.Flusher)
	started := false
	completed := false
	timer := time.AfterFunc(firstEventTimeout, func() {
		if !started {
			_ = source.Close()
		}
	})
	defer timer.Stop()
	for scanner.Scan() {
		line := scanner.Text()
		payload, ok := sseDataPayload(line)
		if !ok || payload == "" {
			continue
		}
		chunk, done, err := providerAdapter.ParseStreamChunk([]byte(payload), model)
		if err != nil {
			return started, err
		}
		if len(chunk) > 0 && string(chunk) != "[DONE]" {
			lines := []string{"data: " + string(chunk)}
			if workspaceID != "" {
				lines, err = s.moderateSSEEvent(ctx, workspaceID, lines, requestID)
				if err != nil {
					return started, err
				}
			}
			if !started {
				timer.Stop()
				start()
				started = true
			}
			for _, out := range lines {
				_, _ = fmt.Fprintln(destination, out)
			}
			_, _ = fmt.Fprintln(destination)
			if flusher != nil {
				flusher.Flush()
			}
		}
		if done || string(chunk) == "[DONE]" {
			if !started {
				timer.Stop()
				start()
				started = true
			}
			fmt.Fprint(destination, "data: [DONE]\n\n")
			if flusher != nil {
				flusher.Flush()
			}
			completed = true
			break
		}
	}
	if err := scanner.Err(); err != nil {
		return started, err
	}
	if !started || !completed {
		return started, io.ErrUnexpectedEOF
	}
	return true, nil
}

func (s *Server) moderateJSONResponse(ctx context.Context, workspaceID string, payload []byte, requestID string) ([]byte, string, error) {
	var response map[string]any
	if err := json.Unmarshal(payload, &response); err != nil {
		return payload, "allow", nil
	}
	choices, _ := response["choices"].([]any)
	for _, rawChoice := range choices {
		choice, _ := rawChoice.(map[string]any)
		message, _ := choice["message"].(map[string]any)
		content, _ := message["content"].(string)
		if content == "" {
			continue
		}
		decision, err := s.Platform.Moderate(ctx, workspaceID, "output", content, requestID)
		if err != nil {
			return nil, "", err
		}
		if decision.Action == "block" {
			return payload, "block", nil
		}
		if decision.Action == "mask" {
			message["content"] = decision.Text
		}
	}
	rewritten, err := json.Marshal(response)
	if err != nil {
		return nil, "", err
	}
	return rewritten, "allow", nil
}

func rewriteCallerModel(payload []byte, model string) []byte {
	var response map[string]json.RawMessage
	if json.Unmarshal(payload, &response) != nil {
		return payload
	}
	encodedModel, err := json.Marshal(model)
	if err != nil {
		return payload
	}
	response["model"] = encodedModel
	rewritten, err := json.Marshal(response)
	if err != nil {
		return payload
	}
	return rewritten
}

func copyChatStream(destination http.ResponseWriter, source io.ReadCloser, model string, firstEventTimeout time.Duration, start func()) (bool, error) {
	return copyChatStreamTransformed(destination, source, model, firstEventTimeout, start, func(lines []string) ([]string, error) { return lines, nil })
}

func copyChatStreamTransformed(destination http.ResponseWriter, source io.ReadCloser, model string, firstEventTimeout time.Duration, start func(), transform func([]string) ([]string, error)) (bool, error) {
	scanner := bufio.NewScanner(source)
	scanner.Buffer(make([]byte, 0, 64<<10), 1<<20)
	flusher, _ := destination.(http.Flusher)
	started := false
	sawDone := false
	sawPayload := false
	var firstEventStarted atomic.Bool
	timer := time.AfterFunc(firstEventTimeout, func() {
		if !firstEventStarted.Load() {
			_ = source.Close()
		}
	})
	defer timer.Stop()
	eventLines := []string{}
	flushEvent := func() error {
		if len(eventLines) == 0 {
			return nil
		}
		lines, event := rewriteSSEEvent(eventLines, model)
		eventLines = nil
		var err error
		lines, err = transform(lines)
		if err != nil {
			return err
		}
		if !event.hasData && !started {
			return nil
		}
		if event.done {
			sawDone = true
		}
		if event.payload {
			sawPayload = true
		}
		if !started {
			if !event.payload {
				return nil
			}
			firstEventStarted.Store(true)
			timer.Stop()
			start()
			started = true
		}
		for _, line := range lines {
			_, _ = fmt.Fprintln(destination, line)
		}
		_, _ = fmt.Fprintln(destination)
		if flusher != nil {
			flusher.Flush()
		}
		return nil
	}
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			if err := flushEvent(); err != nil {
				return started, err
			}
			continue
		}
		eventLines = append(eventLines, line)
	}
	if err := scanner.Err(); err != nil {
		return started, err
	}
	if err := flushEvent(); err != nil {
		return started, err
	}
	if !started || !sawPayload {
		return false, io.ErrUnexpectedEOF
	}
	if !sawDone {
		return true, io.ErrUnexpectedEOF
	}
	return true, nil
}

func (s *Server) moderateSSEEvent(ctx context.Context, workspaceID string, lines []string, requestID string) ([]string, error) {
	for index, line := range lines {
		payload, ok := sseDataPayload(line)
		if !ok || payload == "" || payload == "[DONE]" {
			continue
		}
		var event map[string]any
		if json.Unmarshal([]byte(payload), &event) != nil {
			continue
		}
		choices, _ := event["choices"].([]any)
		changed := false
		for _, rawChoice := range choices {
			choice, _ := rawChoice.(map[string]any)
			delta, _ := choice["delta"].(map[string]any)
			content, _ := delta["content"].(string)
			if content == "" {
				continue
			}
			decision, err := s.Platform.Moderate(ctx, workspaceID, "output", content, requestID)
			if err != nil {
				return nil, err
			}
			if decision.Action == "block" {
				return nil, errModerationBlocked
			}
			if decision.Action == "mask" {
				delta["content"] = decision.Text
				changed = true
			}
		}
		if changed {
			encoded, err := json.Marshal(event)
			if err != nil {
				return nil, err
			}
			lines[index] = "data: " + string(encoded)
		}
	}
	return lines, nil
}

func rewriteSSEEvent(lines []string, model string) ([]string, sseEventState) {
	dataIndexes := map[int]bool{}
	dataParts := []string{}
	firstDataIndex := -1
	for index, line := range lines {
		payload, ok := sseDataPayload(line)
		if !ok {
			continue
		}
		if firstDataIndex == -1 {
			firstDataIndex = index
		}
		dataIndexes[index] = true
		dataParts = append(dataParts, payload)
	}
	if firstDataIndex == -1 {
		return lines, sseEventState{}
	}
	payload := strings.Join(dataParts, "\n")
	event := sseEventState{hasData: true, payload: payload != ""}
	if payload == "[DONE]" {
		event.payload = false
		event.done = true
		return lines, event
	}
	rewritten := rewriteCallerModel([]byte(payload), model)
	if bytes.Equal(rewritten, []byte(payload)) {
		return lines, event
	}
	output := make([]string, 0, len(lines))
	for index, line := range lines {
		if !dataIndexes[index] {
			output = append(output, line)
			continue
		}
		if index == firstDataIndex {
			output = append(output, "data: "+string(rewritten))
		}
	}
	return output, event
}

func sseDataPayload(line string) (string, bool) {
	if !strings.HasPrefix(line, "data:") {
		return "", false
	}
	payload := strings.TrimPrefix(line, "data:")
	if strings.HasPrefix(payload, " ") {
		payload = payload[1:]
	}
	return payload, true
}

func minDuration(left, right time.Duration) time.Duration {
	if left < right {
		return left
	}
	return right
}

func (s *Server) embeddings(w http.ResponseWriter, r *http.Request) {
	started := time.Now()
	requestID := requestID(r)
	var body EmbeddingRequest
	if err := decodeJSON(r.Body, &body); err != nil || body.Model == "" {
		writeOpenAIError(w, http.StatusBadRequest, "E00001", "model and input are required")
		return
	}
	route, err := s.authenticate(r, body.Model)
	if err != nil {
		s.writeResolveError(w, err)
		return
	}
	inputBytes, _ := json.Marshal(body.Input)
	promptTokens := estimateTokens(string(inputBytes))
	if !s.enforceRateLimit(w, r, route, promptTokens) {
		return
	}
	channel, statusCode := s.dispatchEmbedding(w, r, route, body, inputBytes, promptTokens)
	if channel.ID == "" {
		return
	}
	s.recordMeter(r.Context(), metering.Record{
		RequestID: requestID, WorkspaceID: route.WorkspaceID, TokenID: route.TokenID,
		ChannelID: channel.ID, Model: body.Model, PromptTokens: promptTokens,
		LatencyMS: time.Since(started).Milliseconds(), StatusCode: statusCode,
	})
}

func (s *Server) dispatchEmbedding(w http.ResponseWriter, r *http.Request, route relay.Route, body EmbeddingRequest, inputBytes []byte, promptTokens int) (relay.Channel, int) {
	attempted := false
	attempts := 0
	for _, channel := range routeChannels(route) {
		if attempts >= 3 {
			break
		}
		if !s.Breaker.Allow(channel.ID) {
			continue
		}
		attempted = true
		attempts++
		if channel.Provider == "mock" {
			digest := sha256.Sum256(inputBytes)
			vector := make([]float64, 16)
			for i := range vector {
				vector[i] = (float64(digest[i]) - 127.5) / 127.5
			}
			embedding := any(vector)
			if body.EncodingFormat == "base64" {
				embedding = encodeEmbeddingBase64(vector)
			}
			setRouteHeaders(w, channel)
			writeJSON(w, http.StatusOK, map[string]any{
				"object": "list", "model": body.Model,
				"data":  []map[string]any{{"object": "embedding", "index": 0, "embedding": embedding}},
				"usage": map[string]int{"prompt_tokens": promptTokens, "total_tokens": promptTokens},
			})
			s.Breaker.Record(channel.ID, false)
			return channel, http.StatusOK
		}
		upstreamBody := body
		if channel.UpstreamModel != "" {
			upstreamBody.Model = channel.UpstreamModel
		}
		statusCode, delivered := s.forwardRaw(w, r, channel, "/embeddings", upstreamBody)
		if delivered {
			s.Breaker.Record(channel.ID, false)
			return channel, statusCode
		}
		s.Breaker.Record(channel.ID, true)
	}
	if !attempted {
		writeOpenAIError(w, http.StatusServiceUnavailable, "E02003", "All matching channels are temporarily circuit-broken")
		return relay.Channel{}, http.StatusServiceUnavailable
	}
	writeOpenAIError(w, http.StatusBadGateway, "E01007", "All upstream channels failed")
	return relay.Channel{}, http.StatusBadGateway
}

func (s *Server) forwardRaw(w http.ResponseWriter, r *http.Request, channel relay.Channel, path string, body any) (int, bool) {
	payload, _ := json.Marshal(body)
	req, err := http.NewRequestWithContext(r.Context(), http.MethodPost, strings.TrimRight(channel.BaseURL, "/")+path, bytes.NewReader(payload))
	if err != nil {
		return http.StatusBadGateway, false
	}
	req.Header.Set("Content-Type", "application/json")
	if channel.APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+channel.APIKey)
	}
	resp, err := (&http.Client{Timeout: 2 * time.Minute, Transport: commonobservability.Transport(nil)}).Do(req)
	if err != nil {
		return http.StatusBadGateway, false
	}
	defer resp.Body.Close()
	if shouldFailover(resp.StatusCode) {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10))
		return resp.StatusCode, false
	}
	setRouteHeaders(w, channel)
	w.Header().Set("Content-Type", resp.Header.Get("Content-Type"))
	w.WriteHeader(resp.StatusCode)
	_, _ = io.Copy(w, resp.Body)
	return resp.StatusCode, true
}

func routeChannels(route relay.Route) []relay.Channel {
	if len(route.Channels) > 0 {
		return route.Channels
	}
	if route.Channel.ID != "" {
		return []relay.Channel{route.Channel}
	}
	return nil
}

func firstChannel(route relay.Route) relay.Channel {
	channels := routeChannels(route)
	if len(channels) == 0 {
		return relay.Channel{}
	}
	return channels[0]
}

func setRouteHeaders(w http.ResponseWriter, channel relay.Channel) {
	w.Header().Set("X-Wama-Channel", channel.ID)
	if channel.Fallback {
		w.Header().Set("X-Wama-Fallback", "true")
	}
	if channel.Pinned {
		w.Header().Set("X-Wama-Routing", "pinned")
		return
	}
	w.Header().Set("X-Wama-Routing", "weighted")
}

func chatRoutePlans(route relay.Route, fallbackEnabled bool) []chatRoutePlan {
	plans := []chatRoutePlan{{Model: "", Channels: routeChannels(route)}}
	if !fallbackEnabled {
		return plans
	}
	for _, fallback := range route.Fallbacks {
		if len(fallback.Channels) == 0 {
			continue
		}
		plans = append(plans, chatRoutePlan{
			Model:    fallback.Model,
			Channels: fallback.Channels,
			Fallback: true,
		})
	}
	return plans
}

func shouldFailover(statusCode int) bool {
	return statusCode == http.StatusUnauthorized ||
		statusCode == http.StatusForbidden ||
		statusCode == http.StatusRequestTimeout ||
		statusCode == http.StatusTooManyRequests ||
		statusCode >= http.StatusInternalServerError
}

func (s *Server) enforceRateLimit(w http.ResponseWriter, r *http.Request, route relay.Route, estimatedTokens int) bool {
	actorKey := "workspace:" + route.WorkspaceID
	if route.TokenID != nil && *route.TokenID != "" {
		actorKey = "token:" + *route.TokenID
	}
	scopes := []relay.RateLimitScope{{
		ActorKey: actorKey, RPMLimit: route.RPMLimit, TPMLimit: route.TPMLimit,
	}}
	if route.GroupID != nil && *route.GroupID != "" {
		scopes = append(scopes, relay.RateLimitScope{
			ActorKey: "group:" + *route.GroupID, RPMLimit: route.GroupRPMLimit, TPMLimit: route.GroupTPMLimit,
		})
	}
	result, err := s.Platform.RateLimitBatch(r.Context(), scopes, estimatedTokens)
	if err != nil {
		s.Logger.Error("rate limit check failed", "actor_key", actorKey, "error", err)
		writeOpenAIError(w, http.StatusServiceUnavailable, "E01007", "Gateway rate limiter is unavailable")
		return false
	}
	if !result.Allowed {
		w.Header().Set("Retry-After", strconv.Itoa(result.RetryAfter))
		writeOpenAIError(w, http.StatusTooManyRequests, "E01005", "Rate limit exceeded")
		return false
	}
	return true
}

func (s *Server) recordMeter(ctx context.Context, value metering.Record) {
	if s.Metering == nil {
		return
	}
	if err := s.Metering.Record(context.WithoutCancel(ctx), value); err != nil {
		if s.Logger != nil {
			s.Logger.Error("metering write failed", "request_id", value.RequestID, "error", err)
		}
	}
}

func (s *Server) writeResolveError(w http.ResponseWriter, err error) {
	var resolveErr *relay.ResolveError
	if errors.As(err, &resolveErr) {
		code := "E01001"
		message := "API key is invalid or unavailable"
		switch resolveErr.Status {
		case http.StatusPaymentRequired:
			code, message = "E01004", "Credit balance is insufficient"
		case http.StatusForbidden:
			code, message = "E01002", "Model is not allowed for this key"
		case http.StatusNotFound:
			code, message = "E01006", "No channel is available for this model"
		}
		writeOpenAIError(w, resolveErr.Status, code, message)
		return
	}
	writeOpenAIError(w, http.StatusBadGateway, "E01007", "Gateway control plane is unavailable")
}

func (s *Server) writeBudgetError(w http.ResponseWriter, err error) {
	var resolveErr *relay.ResolveError
	if errors.As(err, &resolveErr) && resolveErr.Status == http.StatusPaymentRequired {
		writeOpenAIError(w, http.StatusPaymentRequired, "E01004", "Credit balance is insufficient")
		return
	}
	writeOpenAIError(w, http.StatusServiceUnavailable, "E01007", "Budget service is unavailable")
}

func mockReply(messages []Message) string {
	last := ""
	contextText := ""
	for _, message := range messages {
		content, ok := message.Content.(string)
		if !ok {
			continue
		}
		if message.Role == "system" && strings.Contains(content, "Attachment context") {
			contextText = content
		}
		if message.Role == "user" {
			last = content
		}
	}
	if strings.TrimSpace(last) == "" {
		return "Please provide a message so I can help."
	}
	if contextText != "" {
		return fmt.Sprintf("I reviewed the attached text and your question. Based on the available context, the key point is: %s", compact(last, 220))
	}
	return fmt.Sprintf("WorkAMA received your request: %s\n\nThis response is generated by the local verification model. Configure an OpenAI-compatible channel to use a production model.", compact(last, 240))
}

func compact(value string, limit int) string {
	value = strings.Join(strings.Fields(value), " ")
	runes := []rune(value)
	if len(runes) <= limit {
		return value
	}
	return string(runes[:limit]) + "..."
}

func estimateMessageTokens(messages []Message) int {
	total := 0
	for _, message := range messages {
		if value, ok := message.Content.(string); ok {
			total += estimateTokens(value) + 4
		}
	}
	if total == 0 {
		return 1
	}
	return total
}

func estimateTokens(value string) int {
	count := utf8.RuneCountInString(value) / 4
	if count < 1 {
		return 1
	}
	return count
}

func encodeEmbeddingBase64(vector []float64) string {
	buffer := make([]byte, len(vector)*4)
	for index, value := range vector {
		binary.LittleEndian.PutUint32(buffer[index*4:], math.Float32bits(float32(value)))
	}
	return base64.StdEncoding.EncodeToString(buffer)
}

func splitRunes(value string, size int) []string {
	runes := []rune(value)
	chunks := make([]string, 0, (len(runes)+size-1)/size)
	for start := 0; start < len(runes); start += size {
		end := start + size
		if end > len(runes) {
			end = len(runes)
		}
		chunks = append(chunks, string(runes[start:end]))
	}
	return chunks
}

func requestID(r *http.Request) string {
	if value := observability.RequestID(r.Context()); value != "" {
		return value
	}
	if value := r.Header.Get("X-Wama-Request-ID"); value != "" {
		return value
	}
	if value := r.Header.Get("X-Request-ID"); value != "" {
		return value
	}
	return fmt.Sprintf("req_%d", time.Now().UnixNano())
}

func decodeJSON(reader io.Reader, target any) error {
	decoder := json.NewDecoder(io.LimitReader(reader, 4<<20))
	return decoder.Decode(target)
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func writeSSE(w io.Writer, value any) {
	payload, _ := json.Marshal(value)
	fmt.Fprintf(w, "data: %s\n\n", payload)
}

func writeOpenAIError(w http.ResponseWriter, status int, code, message string) {
	writeJSON(w, status, map[string]any{
		"error": map[string]any{"message": message, "type": "workama_error", "param": nil, "code": code},
	})
}
