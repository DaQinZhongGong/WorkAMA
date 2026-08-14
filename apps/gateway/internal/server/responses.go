package server

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"net/http"
	"net/http/httptest"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"

	"github.com/workama/workama/apps/gateway/internal/metering"
	"github.com/workama/workama/apps/gateway/internal/relay"
)

const (
	// Keep a real cancellation window between the accepted background request and upstream execution.
	// A 20ms window is shorter than a normal HTTP round trip from the CLI or browser.
	responseBackgroundStartDelay               = 250 * time.Millisecond
	responseSemanticCacheEnv                   = "WORKAMA_RESPONSES_SEMANTIC_CACHE"
	responseSemanticCacheWorkspacesEnv         = "WORKAMA_RESPONSES_SEMANTIC_CACHE_WORKSPACES"
	responseSemanticCacheCandidateEnv          = "WORKAMA_RESPONSES_SEMANTIC_CACHE_CANDIDATE"
	responseSemanticCachePGVectorEnv           = "WORKAMA_RESPONSES_SEMANTIC_CACHE_PGVECTOR"
	responseSemanticCachePGVectorWorkspacesEnv = "WORKAMA_RESPONSES_SEMANTIC_CACHE_PGVECTOR_WORKSPACES"
	responseSemanticCacheThresholdEnv          = "WORKAMA_RESPONSES_SEMANTIC_CACHE_SIMILARITY_THRESHOLD"
	responseSemanticCacheMaxCandidatesEnv      = "WORKAMA_RESPONSES_SEMANTIC_CACHE_MAX_CANDIDATES"
	responseSemanticCacheTTL                   = 5 * time.Minute
	responseSemanticCacheRepositoryTimeout     = 75 * time.Millisecond
	responseSemanticCacheMaxSize               = 256
	responseSemanticCacheMaxText               = 64 << 10
	responseSemanticCacheDefaultThreshold      = 0.97
	responseSemanticCacheEmbeddingDimensions   = 128
	responseSemanticCacheDefaultMaxCandidates  = 64
	responseMetadataMaxEntries                 = 16
	responseMetadataMaxKeyLength               = 64
	responseMetadataMaxValueLen                = 512
)

var (
	errResponseInputBlocked     = errors.New("response input blocked")
	errResponseInputUnsupported = errors.New("response input item is unsupported")
	errResponseInputMultimodal  = errors.New("response multimodal input is unsupported")
	errResponseInputEmpty       = errors.New("response input is empty")
	errResponseMetadataInvalid  = errors.New("response metadata is invalid")
	errPreviousResponseNotFound = errors.New("previous response not found")
	errPreviousResponsePending  = errors.New("previous response is not completed")
)

type ResponsesRequest struct {
	Model              string            `json:"model"`
	Input              any               `json:"input"`
	Instructions       string            `json:"instructions,omitempty"`
	PromptVariables    map[string]string `json:"prompt_variables,omitempty"`
	Temperature        *float64          `json:"temperature,omitempty"`
	Tools              []any             `json:"tools,omitempty"`
	ToolChoice         any               `json:"tool_choice,omitempty"`
	SemanticCache      *bool             `json:"semantic_cache,omitempty"`
	Region             string            `json:"region,omitempty"`
	GuardPolicyVersion string            `json:"guard_policy_version,omitempty"`
	DataClassification string            `json:"data_classification,omitempty"`
	ResponseFormat     any               `json:"response_format,omitempty"`
	Capability         string            `json:"capability,omitempty"`
	Capabilities       []string          `json:"capabilities,omitempty"`
	SideEffect         bool              `json:"side_effect,omitempty"`
	SideEffects        []any             `json:"side_effects,omitempty"`
	Background         bool              `json:"background,omitempty"`
	Stream             bool              `json:"stream,omitempty"`
	MaxOutputTokens    *int              `json:"max_output_tokens,omitempty"`
	WamaFallback       *bool             `json:"wama_fallback,omitempty"`
	Metadata           map[string]string `json:"metadata,omitempty"`
	PreviousResponseID string            `json:"previous_response_id,omitempty"`
}

type responseObject struct {
	ID                 string                    `json:"id"`
	Object             string                    `json:"object"`
	CreatedAt          int64                     `json:"created_at"`
	Status             string                    `json:"status"`
	Model              string                    `json:"model"`
	Output             []responseOutputItem      `json:"output"`
	OutputText         string                    `json:"output_text"`
	Error              *responseError            `json:"error"`
	IncompleteDetails  *responseIncompleteDetail `json:"incomplete_details"`
	Usage              *responseUsage            `json:"usage"`
	Metadata           map[string]string         `json:"metadata"`
	PreviousResponseID string                    `json:"previous_response_id,omitempty"`
}

type responseOutputItem struct {
	ID      string                `json:"id"`
	Type    string                `json:"type"`
	Status  string                `json:"status"`
	Role    string                `json:"role"`
	Content []responseOutputBlock `json:"content"`
}

type responseOutputBlock struct {
	Type        string `json:"type"`
	Text        string `json:"text"`
	Annotations []any  `json:"annotations"`
	Logprobs    []any  `json:"logprobs,omitempty"`
}

type responseError struct {
	Type    string `json:"type"`
	Code    string `json:"code"`
	Message string `json:"message"`
}

type responseIncompleteDetail struct {
	Reason string `json:"reason"`
}

type responseUsage struct {
	InputTokens  int `json:"input_tokens"`
	OutputTokens int `json:"output_tokens"`
	TotalTokens  int `json:"total_tokens"`
}

type responseRecord struct {
	workspaceID             string
	model                   string
	requestID               string
	semanticCacheKey        string
	semanticCacheScope      responseSemanticCacheScope
	semanticCacheEmbedding  []float64
	semanticCacheSafe       bool
	semanticCacheHit        bool
	semanticCacheProvenance string
	semanticCacheSimilarity float64
	background              bool
	expiresAt               time.Time
	object                  responseObject
	chatBody                ChatRequest
	promptTokens            int
	cancel                  context.CancelFunc
	releaseOnce             sync.Once
	meterOnce               sync.Once
}

type responseRegistry struct {
	mu                              sync.RWMutex
	records                         map[string]*responseRecord
	semanticCache                   map[string]responseSemanticCacheEntry
	semanticCacheRepository         responseSemanticCacheRepository
	persistence                     responsePersistence
	fallbackPersistence             responsePersistence
	ttl                             time.Duration
	now                             func() time.Time
	logger                          *slog.Logger
	persistenceWarnOnce             sync.Once
	semanticCacheRepositoryWarnOnce sync.Once
	ensureOnce                      sync.Once
}

type ResponseSemanticCacheEntry struct {
	Text             string
	CompletionTokens int
	CreatedAt        time.Time
	ExpiresAt        time.Time
	Scope            responseSemanticCacheScope
	Embedding        []float64
}

type responseSemanticCacheEntry = ResponseSemanticCacheEntry

type ResponseSemanticCacheScope struct {
	WorkspaceID        string
	Model              string
	Provider           string
	ChannelID          string
	UpstreamModel      string
	Capability         string
	PromptID           string
	PromptVersion      int
	PromptChecksum     string
	GuardPolicyVersion string
	DataClassification string
	OutputSignature    string
	Region             string
}

type responseSemanticCacheScope = ResponseSemanticCacheScope

type responseSemanticCacheContext struct {
	Key       string
	Scope     responseSemanticCacheScope
	Embedding []float64
}

type ResponseSemanticCacheLookupRequest struct {
	Key           string
	Scope         ResponseSemanticCacheScope
	Embedding     []float64
	Threshold     float64
	MaxCandidates int
	Now           time.Time
}

type responseSemanticCacheRepositoryQuery = ResponseSemanticCacheLookupRequest

type ResponseSemanticCacheCandidate struct {
	Key        string
	Entry      ResponseSemanticCacheEntry
	Similarity float64
}

type responseSemanticCacheRepositoryCandidate = ResponseSemanticCacheCandidate

type ResponseSemanticCacheLookupResult struct {
	Exact      *ResponseSemanticCacheEntry
	Candidates []ResponseSemanticCacheCandidate
}

type responseSemanticCacheRepositoryResult = ResponseSemanticCacheLookupResult

type responseSemanticCacheRepository interface {
	Lookup(context.Context, ResponseSemanticCacheLookupRequest) (ResponseSemanticCacheLookupResult, error)
	Put(context.Context, string, ResponseSemanticCacheEntry) error
}

// ResponseSemanticCacheRepository is intentionally an injection boundary. The
// bundled pgx adapter is the production default, while deployments can provide
// their own SQL/pgvector pool without changing the request or Platform API.
type ResponseSemanticCacheRepository = responseSemanticCacheRepository

type ResponseSemanticCacheSQLRows interface {
	Next() bool
	Scan(...any) error
	Close() error
	Err() error
}

type ResponseSemanticCacheSQLExecutor interface {
	QueryContext(context.Context, string, ...any) (ResponseSemanticCacheSQLRows, error)
	ExecContext(context.Context, string, ...any) error
}

type parsedResponseInput struct {
	Text       string
	Normalized string
}

type responseExecution struct {
	text             string
	completionTokens int
	channel          relay.Channel
	statusCode       int
}

func (s *Server) responseStore() *responseRegistry {
	store := &s.responses
	store.ensure(s.Logger)
	store.ensureProductionSemanticCacheRepository()
	store.mu.Lock()
	if store.records == nil {
		store.records = make(map[string]*responseRecord)
	}
	store.mu.Unlock()
	return store
}

func (s *Server) createResponse(w http.ResponseWriter, r *http.Request) {
	var body ResponsesRequest
	if err := decodeJSON(r.Body, &body); err != nil {
		writeOpenAIError(w, http.StatusBadRequest, "E00001", "Invalid JSON request")
		return
	}
	if body.Model == "" || body.Input == nil {
		writeOpenAIError(w, http.StatusBadRequest, "E00001", "model and input are required")
		return
	}
	requestedModel := body.Model
	promptID := ""
	if strings.HasPrefix(requestedModel, "prompt:") {
		promptID = strings.TrimSpace(strings.TrimPrefix(requestedModel, "prompt:"))
		if promptID == "" {
			writeOpenAIError(w, http.StatusBadRequest, "E00001", "prompt model reference is empty")
			return
		}
		body.Model = "workama-chat"
	}
	if body.Stream && body.Background {
		writeOpenAIError(w, http.StatusBadRequest, "E00001", "background Responses cannot also request streaming")
		return
	}
	parsedInput, err := parseResponseInput(body.Input)
	if err != nil {
		message := "input must contain supported text content"
		if errors.Is(err, errResponseInputMultimodal) {
			message = "image and audio input items are not supported in this compatibility slice"
		}
		writeOpenAIError(w, http.StatusBadRequest, "E00001", message)
		return
	}
	metadata, err := normalizeResponseMetadata(body.Metadata)
	if err != nil {
		writeOpenAIError(w, http.StatusBadRequest, "E00001", "metadata is invalid or contains restricted fields")
		return
	}
	route, err := s.authenticate(r, body.Model)
	if err != nil {
		s.writeResolveError(w, err)
		return
	}
	promptVersion := 0
	promptChecksum := ""
	if promptID != "" {
		if s.Platform == nil {
			writeOpenAIError(w, http.StatusBadGateway, "E01007", "Prompt control plane is unavailable")
			return
		}
		resolution, resolveErr := s.Platform.ResolvePrompt(r.Context(), route.WorkspaceID, promptID, promptRolloutVariables(body.PromptVariables, route))
		if resolveErr != nil {
			var promptResolveErr *relay.ResolveError
			if errors.As(resolveErr, &promptResolveErr) {
				if promptResolveErr.Status == http.StatusNotFound {
					writeOpenAIError(w, http.StatusNotFound, "E00004", "Published prompt not found")
				} else if promptResolveErr.Status == http.StatusUnprocessableEntity {
					writeOpenAIError(w, http.StatusUnprocessableEntity, "E00001", "Prompt variables are invalid or incomplete")
				} else {
					writeOpenAIError(w, http.StatusBadGateway, "E01007", "Prompt control plane is unavailable")
				}
			} else {
				writeOpenAIError(w, http.StatusBadGateway, "E01007", "Prompt control plane is unavailable")
			}
			return
		}
		promptVersion = resolution.Version
		promptChecksum = resolution.Checksum
		if strings.TrimSpace(body.Instructions) == "" {
			body.Instructions = resolution.Content
		} else {
			body.Instructions = resolution.Content + "\n" + body.Instructions
		}
		metadata["wama_prompt_id"] = resolution.ID
		metadata["wama_prompt_version"] = strconv.Itoa(resolution.Version)
		metadata["wama_prompt_checksum"] = resolution.Checksum
	}
	previousText, err := s.previousResponseText(route.WorkspaceID, body.PreviousResponseID)
	if err != nil {
		if errors.Is(err, errPreviousResponseNotFound) {
			writeOpenAIError(w, http.StatusNotFound, "E00004", "Previous response not found")
		} else if errors.Is(err, errPreviousResponsePending) {
			writeOpenAIError(w, http.StatusConflict, "E00004", "Previous response is not completed")
		} else {
			writeOpenAIError(w, http.StatusBadRequest, "E00001", "previous_response_id is invalid")
		}
		return
	}
	requestID := requestID(r)
	promptText := parsedInput.Text
	if previousText != "" {
		promptText += "\n" + previousText
	}
	promptTokens := estimateTokens(promptText + "\n" + body.Instructions)
	if !s.enforceRateLimit(w, r, route, promptTokens) {
		return
	}
	estimatedTokens := promptTokens + 1024
	if body.MaxOutputTokens != nil && *body.MaxOutputTokens > 0 {
		estimatedTokens = promptTokens + *body.MaxOutputTokens
	}
	if _, err := s.Platform.Reserve(r.Context(), requestID, route.WorkspaceID, body.Model, estimatedTokens); err != nil {
		s.writeBudgetError(w, err)
		return
	}
	moderatedInput, moderationErr := s.moderateResponseInput(r.Context(), route.WorkspaceID, parsedInput.Text, requestID)
	if moderationErr != nil {
		s.releaseResponseReservation(requestID, r.Context())
		if errors.Is(moderationErr, errResponseInputBlocked) {
			writeOpenAIError(w, http.StatusBadRequest, "E01008", "Input was blocked by workspace policy")
		} else {
			writeOpenAIError(w, http.StatusServiceUnavailable, "E01007", "Content safety service is unavailable")
		}
		return
	}
	moderatedInstructions := body.Instructions
	if strings.TrimSpace(body.Instructions) != "" {
		moderatedInstructions, moderationErr = s.moderateResponseInput(r.Context(), route.WorkspaceID, body.Instructions, requestID)
		if moderationErr != nil {
			s.releaseResponseReservation(requestID, r.Context())
			if errors.Is(moderationErr, errResponseInputBlocked) {
				writeOpenAIError(w, http.StatusBadRequest, "E01008", "Input was blocked by workspace policy")
			} else {
				writeOpenAIError(w, http.StatusServiceUnavailable, "E01007", "Content safety service is unavailable")
			}
			return
		}
	}
	body.Instructions = moderatedInstructions
	chatBody := responseChatRequestWithPrevious(body, moderatedInput, previousText)
	semanticCacheKey := ""
	semanticCacheScope := responseSemanticCacheScope{}
	semanticCacheEmbedding := []float64(nil)
	semanticCacheSafe := false
	if responseSemanticCacheEligible(body, route) && responseSemanticCacheRouteAllowed(route) {
		cacheContext := responseSemanticCacheContextForRequest(route, body, normalizedResponseCacheInput(moderatedInput, body.Instructions, previousText), promptID, promptVersion, promptChecksum)
		semanticCacheKey = cacheContext.Key
		semanticCacheScope = cacheContext.Scope
		semanticCacheEmbedding = cacheContext.Embedding
		semanticCacheSafe = true
	}
	responseID := newResponseID()
	workerContext, cancel := context.WithCancel(context.WithoutCancel(r.Context()))
	store := s.responseStore()
	createdAt := store.now()
	responseStatus := "in_progress"
	if body.Background {
		responseStatus = "queued"
	}
	record := &responseRecord{
		workspaceID:            route.WorkspaceID,
		model:                  body.Model,
		requestID:              requestID,
		semanticCacheKey:       semanticCacheKey,
		semanticCacheScope:     semanticCacheScope,
		semanticCacheEmbedding: semanticCacheEmbedding,
		semanticCacheSafe:      semanticCacheSafe,
		background:             body.Background,
		expiresAt:              createdAt.Add(store.ttl),
		chatBody:               chatBody,
		promptTokens:           promptTokens,
		cancel:                 cancel,
		object: responseObject{
			ID: responseID, Object: "response", CreatedAt: createdAt.Unix(),
			Status: responseStatus, Model: body.Model, Output: []responseOutputItem{},
			Metadata: metadata, PreviousResponseID: body.PreviousResponseID,
		},
	}
	store.mu.Lock()
	store.records[responseID] = record
	store.mu.Unlock()
	store.persist(record)
	if body.Background {
		writeJSON(w, http.StatusAccepted, s.responseSnapshot(record))
		go s.runBackgroundResponse(workerContext, record, route, chatBody, promptTokens)
		return
	}
	if cached, provenance, similarity, ok := s.responseSemanticCacheLookup(record); ok {
		s.markResponseCacheHit(record, provenance, similarity)
		w.Header().Set("x-wama-cache", "hit")
		w.Header().Set("x-wama-cache-provenance", provenance)
		if provenance == "semantic" {
			w.Header().Set("x-wama-cache-similarity", strconv.FormatFloat(similarity, 'f', 6, 64))
		}
		s.completeResponse(record, cached.Text, promptTokens, cached.CompletionTokens)
		s.meterResponse(record, firstChannel(route), promptTokens, cached.CompletionTokens, http.StatusOK)
		if body.Stream {
			writeResponseStream(w, s.responseSnapshot(record))
		} else {
			writeJSON(w, http.StatusOK, s.responseSnapshot(record))
		}
		return
	}

	execution := s.executeResponse(workerContext, route, chatBody, requestID, promptTokens)
	cancel()
	if execution.channel.ID == "" || execution.statusCode >= http.StatusBadRequest {
		s.failResponse(record, "response_execution_failed", "The model response could not be completed")
		s.releaseResponseReservation(requestID, r.Context())
		writeOpenAIError(w, http.StatusBadGateway, "E01007", "The model response could not be completed")
		return
	}
	s.completeResponse(record, execution.text, promptTokens, execution.completionTokens)
	s.responseSemanticCachePut(record, execution.text, execution.completionTokens)
	s.meterResponse(record, execution.channel, promptTokens, execution.completionTokens, execution.statusCode)
	if body.Stream {
		writeResponseStream(w, s.responseSnapshot(record))
	} else {
		writeJSON(w, http.StatusOK, s.responseSnapshot(record))
	}
}

func writeResponseStream(w http.ResponseWriter, response responseObject) {
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.WriteHeader(http.StatusOK)
	flusher, _ := w.(http.Flusher)
	emit := func(event string, payload any) {
		_, _ = fmt.Fprintf(w, "event: %s\n", event)
		writeSSE(w, payload)
		if flusher != nil {
			flusher.Flush()
		}
	}
	emit("response.created", map[string]any{"type": "response.created", "response": response})
	if response.OutputText != "" {
		emit("response.output_text.delta", map[string]any{
			"type": "response.output_text.delta", "response_id": response.ID, "delta": response.OutputText,
		})
	}
	emit("response.completed", map[string]any{"type": "response.completed", "response": response})
	_, _ = fmt.Fprint(w, "data: [DONE]\n\n")
	if flusher != nil {
		flusher.Flush()
	}
}

func (s *Server) getResponse(w http.ResponseWriter, r *http.Request) {
	responseID := r.PathValue("response_id")
	record, ok := s.responseStore().get(responseID)
	if !ok {
		writeOpenAIError(w, http.StatusNotFound, "E00004", "Response not found")
		return
	}
	if !s.responseBelongsToCaller(w, r, record) {
		return
	}
	if s.responseCacheWasHit(record) {
		provenance, similarity := s.responseCacheHitDetails(record)
		w.Header().Set("x-wama-cache", "hit")
		w.Header().Set("x-wama-cache-provenance", provenance)
		if provenance == "semantic" {
			w.Header().Set("x-wama-cache-similarity", strconv.FormatFloat(similarity, 'f', 6, 64))
		}
	}
	writeJSON(w, http.StatusOK, s.responseSnapshot(record))
}

func (s *Server) cancelResponse(w http.ResponseWriter, r *http.Request) {
	responseID := r.PathValue("response_id")
	record, ok := s.responseStore().get(responseID)
	if !ok {
		writeOpenAIError(w, http.StatusNotFound, "E00004", "Response not found")
		return
	}
	if !s.responseBelongsToCaller(w, r, record) {
		return
	}
	if !s.cancelResponseRecord(record) {
		writeOpenAIError(w, http.StatusConflict, "E00004", "Response is no longer cancellable")
		return
	}
	writeJSON(w, http.StatusOK, s.responseSnapshot(record))
}

func (s *Server) responseBelongsToCaller(w http.ResponseWriter, r *http.Request, record *responseRecord) bool {
	route, err := s.authenticate(r, record.model)
	if err != nil {
		s.writeResolveError(w, err)
		return false
	}
	if route.WorkspaceID != record.workspaceID {
		writeOpenAIError(w, http.StatusNotFound, "E00004", "Response not found")
		return false
	}
	return true
}

func (s *Server) runBackgroundResponse(ctx context.Context, record *responseRecord, route relay.Route, body ChatRequest, promptTokens int) {
	timer := time.NewTimer(responseBackgroundStartDelay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		s.releaseResponseRecord(record, context.Background())
		return
	case <-timer.C:
	}
	if !s.markResponseInProgress(record) {
		s.releaseResponseRecord(record, context.Background())
		return
	}
	if cached, provenance, similarity, ok := s.responseSemanticCacheLookup(record); ok {
		s.markResponseCacheHit(record, provenance, similarity)
		if s.completeResponse(record, cached.Text, promptTokens, cached.CompletionTokens) {
			s.meterResponse(record, firstChannel(route), promptTokens, cached.CompletionTokens, http.StatusOK)
		}
		return
	}
	execution := s.executeResponse(ctx, route, body, record.requestID, promptTokens)
	if execution.channel.ID == "" || execution.statusCode >= http.StatusBadRequest {
		if !s.failResponse(record, "response_execution_failed", "The model response could not be completed") {
			return
		}
		s.releaseResponseRecord(record, context.Background())
		return
	}
	if !s.completeResponse(record, execution.text, promptTokens, execution.completionTokens) {
		s.releaseResponseRecord(record, context.Background())
		return
	}
	s.responseSemanticCachePut(record, execution.text, execution.completionTokens)
	s.meterResponse(record, execution.channel, promptTokens, execution.completionTokens, execution.statusCode)
}

func (s *Server) executeResponse(ctx context.Context, route relay.Route, body ChatRequest, requestID string, promptTokens int) responseExecution {
	request := httptest.NewRequest(http.MethodPost, "http://gateway.local/v1/chat/completions", nil)
	request = request.WithContext(context.WithValue(ctx, moderationWorkspaceKey{}, route.WorkspaceID))
	recorder := httptest.NewRecorder()
	channel, statusCode, completionTokens := s.dispatchChat(recorder, request, route, body, requestID, promptTokens)
	if channel.ID == "" || recorder.Code >= http.StatusBadRequest || statusCode >= http.StatusBadRequest {
		return responseExecution{channel: relay.Channel{}, statusCode: statusCode}
	}
	var chat map[string]any
	if err := json.Unmarshal(recorder.Body.Bytes(), &chat); err != nil {
		return responseExecution{channel: relay.Channel{}, statusCode: http.StatusBadGateway}
	}
	text := ""
	if choices, ok := chat["choices"].([]any); ok && len(choices) > 0 {
		if choice, ok := choices[0].(map[string]any); ok {
			if message, ok := choice["message"].(map[string]any); ok {
				text, _ = message["content"].(string)
			}
		}
	}
	if completionTokens == 0 {
		completionTokens = estimateTokens(text)
	}
	return responseExecution{text: text, completionTokens: completionTokens, channel: channel, statusCode: statusCode}
}

func (s *Server) moderateResponseInput(ctx context.Context, workspaceID, text, requestID string) (string, error) {
	decision, err := s.Platform.Moderate(ctx, workspaceID, "input", text, requestID)
	if err != nil {
		if s.Logger != nil {
			s.Logger.Error("response input moderation failed", "request_id", requestID, "error", err)
		}
		return "", err
	}
	if decision.Action == "block" {
		return "", errResponseInputBlocked
	}
	if decision.Action == "mask" {
		return decision.Text, nil
	}
	return text, nil
}

func responseChatRequest(body ResponsesRequest, inputText string) ChatRequest {
	return responseChatRequestWithPrevious(body, inputText, "")
}

func responseChatRequestWithPrevious(body ResponsesRequest, inputText, previousText string) ChatRequest {
	messages := make([]Message, 0, 2)
	if strings.TrimSpace(body.Instructions) != "" {
		messages = append(messages, Message{Role: "system", Content: body.Instructions})
	}
	if strings.TrimSpace(previousText) != "" {
		messages = append(messages, Message{Role: "assistant", Content: previousText})
	}
	messages = append(messages, Message{Role: "user", Content: inputText})
	return ChatRequest{
		Model: body.Model, Messages: messages, MaxTokens: body.MaxOutputTokens,
		Temperature: body.Temperature, Tools: body.Tools, ToolChoice: body.ToolChoice,
		WamaFallback: body.WamaFallback,
	}
}

func responseInputText(input any) (string, error) {
	parsed, err := parseResponseInput(input)
	if err != nil {
		return "", err
	}
	return parsed.Text, nil
}

func parseResponseInput(input any) (parsedResponseInput, error) {
	parts := make([]string, 0, 4)
	if err := appendResponseInputText(&parts, input); err != nil {
		return parsedResponseInput{}, err
	}
	cleaned := make([]string, 0, len(parts))
	for _, part := range parts {
		if trimmed := strings.TrimSpace(part); trimmed != "" {
			cleaned = append(cleaned, trimmed)
		}
	}
	if len(cleaned) == 0 {
		return parsedResponseInput{}, errResponseInputEmpty
	}
	text := strings.Join(cleaned, "\n")
	return parsedResponseInput{Text: text, Normalized: normalizeResponseText(text)}, nil
}

func appendResponseInputText(parts *[]string, value any) error {
	switch typed := value.(type) {
	case string:
		*parts = append(*parts, typed)
	case []any:
		for _, item := range typed {
			if err := appendResponseInputText(parts, item); err != nil {
				return err
			}
		}
	case map[string]any:
		return appendResponseInputObject(parts, typed)
	default:
		return errResponseInputUnsupported
	}
	return nil
}

func appendResponseInputObject(parts *[]string, object map[string]any) error {
	if rawType, ok := object["type"]; ok {
		typeName, ok := rawType.(string)
		if !ok {
			return errResponseInputUnsupported
		}
		switch strings.ToLower(strings.TrimSpace(typeName)) {
		case "", "message", "text", "input_text":
		case "image", "image_url", "input_image", "audio", "audio_url", "input_audio", "file", "file_reference":
			return errResponseInputMultimodal
		default:
			return errResponseInputUnsupported
		}
	}

	for key := range object {
		switch key {
		case "type", "role", "id", "name", "status":
			continue
		case "image", "image_url", "input_image", "audio", "audio_url", "input_audio", "file", "file_id", "file_reference", "data":
			return errResponseInputMultimodal
		case "text", "input_text", "content":
		default:
			return errResponseInputUnsupported
		}
	}
	// Keep mixed text/content forms deterministic even though JSON objects are decoded into maps.
	for _, key := range []string{"text", "input_text", "content"} {
		if item, ok := object[key]; ok {
			if err := appendResponseInputText(parts, item); err != nil {
				return err
			}
		}
	}
	return nil
}

func normalizeResponseText(value string) string {
	return strings.ToLower(strings.Join(strings.Fields(strings.TrimSpace(value)), " "))
}

func normalizeResponseMetadata(metadata map[string]string) (map[string]string, error) {
	if len(metadata) > responseMetadataMaxEntries {
		return nil, errResponseMetadataInvalid
	}
	result := make(map[string]string, len(metadata))
	for key, value := range metadata {
		trimmedKey := strings.TrimSpace(key)
		if trimmedKey == "" || trimmedKey != key || len(trimmedKey) > responseMetadataMaxKeyLength || len(value) > responseMetadataMaxValueLen || responseMetadataKeyRestricted(trimmedKey) {
			return nil, errResponseMetadataInvalid
		}
		result[trimmedKey] = value
	}
	return result, nil
}

func responseMetadataKeyRestricted(key string) bool {
	normalized := strings.ToLower(strings.NewReplacer("-", "_", ".", "_").Replace(key))
	for _, fragment := range []string{"api_key", "apikey", "authorization", "bearer", "credential", "password", "private_key", "secret", "token", "cookie"} {
		if strings.Contains(normalized, fragment) {
			return true
		}
	}
	return false
}

func (s *Server) previousResponseText(workspaceID, responseID string) (string, error) {
	responseID = strings.TrimSpace(responseID)
	if responseID == "" {
		return "", nil
	}
	if len(responseID) > 128 {
		return "", errResponseInputUnsupported
	}
	record, ok := s.responseStore().get(responseID)
	if !ok || record.workspaceID != workspaceID {
		return "", errPreviousResponseNotFound
	}
	previous := s.responseSnapshot(record)
	if previous.Status != "completed" || strings.TrimSpace(previous.OutputText) == "" {
		return "", errPreviousResponsePending
	}
	return previous.OutputText, nil
}

func normalizedResponseCacheInput(inputText, instructions, previousText string) string {
	return strings.Join([]string{
		"input=" + normalizeResponseText(inputText),
		"instructions=" + normalizeResponseText(instructions),
		"previous=" + normalizeResponseText(previousText),
	}, "\n")
}

func promptRolloutVariables(variables map[string]string, route relay.Route) map[string]string {
	result := make(map[string]string, len(variables)+1)
	for key, value := range variables {
		result[key] = value
	}
	rolloutKey := route.WorkspaceID
	if route.TokenID != nil && strings.TrimSpace(*route.TokenID) != "" {
		rolloutKey = *route.TokenID
	} else if route.GroupID != nil && strings.TrimSpace(*route.GroupID) != "" {
		rolloutKey = *route.GroupID
	}
	result["__wama_rollout_key"] = rolloutKey
	return result
}

func responseSemanticCacheKey(workspaceID, model, normalizedInput, promptID string, promptVersion int) string {
	return responseSemanticCacheDigest([]string{
		"legacy-v1", workspaceID, model, promptID, strconv.Itoa(promptVersion), normalizedInput,
	})
}

func responseSemanticCacheKeyForRequest(route relay.Route, body ResponsesRequest, normalizedInput, promptID string, promptVersion int) string {
	return responseSemanticCacheKeyForRequestWithPromptChecksum(route, body, normalizedInput, promptID, promptVersion, "")
}

func responseSemanticCacheKeyForRequestWithPromptChecksum(route relay.Route, body ResponsesRequest, normalizedInput, promptID string, promptVersion int, promptChecksum string) string {
	return responseSemanticCacheContextForRequest(route, body, normalizedInput, promptID, promptVersion, promptChecksum).Key
}

func responseSemanticCacheContextForRequest(route relay.Route, body ResponsesRequest, normalizedInput, promptID string, promptVersion int, promptChecksum string) responseSemanticCacheContext {
	tokenID := ""
	if route.TokenID != nil {
		tokenID = *route.TokenID
	}
	groupID := ""
	if route.GroupID != nil {
		groupID = *route.GroupID
	}
	fallback := ""
	if body.WamaFallback != nil {
		fallback = strconv.FormatBool(*body.WamaFallback)
	}
	channel := firstChannel(route)
	scope := responseSemanticCacheScope{
		WorkspaceID:        route.WorkspaceID,
		Model:              body.Model,
		Provider:           channel.Provider,
		ChannelID:          channel.ID,
		UpstreamModel:      channel.UpstreamModel,
		Capability:         responseCapability(body),
		PromptID:           promptID,
		PromptVersion:      promptVersion,
		PromptChecksum:     strings.TrimSpace(promptChecksum),
		GuardPolicyVersion: strings.TrimSpace(body.GuardPolicyVersion),
		DataClassification: strings.ToUpper(strings.TrimSpace(body.DataClassification)),
		OutputSignature:    responseOutputSignature(body),
		Region:             strings.TrimSpace(body.Region),
	}
	key := responseSemanticCacheDigest([]string{
		"semantic-v3",
		route.WorkspaceID,
		tokenID,
		groupID,
		body.Model,
		channel.ID,
		channel.Provider,
		channel.BaseURL,
		channel.UpstreamModel,
		scope.Capability,
		scope.Region,
		scope.GuardPolicyVersion,
		scope.DataClassification,
		promptID,
		strconv.Itoa(promptVersion),
		scope.PromptChecksum,
		fallback,
		scope.OutputSignature,
		normalizedInput,
	})
	return responseSemanticCacheContext{Key: key, Scope: scope, Embedding: responseSemanticEmbedding(normalizedInput)}
}

func responseOutputSignature(body ResponsesRequest) string {
	responseFormat := ""
	if body.ResponseFormat != nil {
		if encoded, err := json.Marshal(body.ResponseFormat); err == nil {
			responseFormat = string(encoded)
		}
	}
	maxOutputTokens := ""
	if body.MaxOutputTokens != nil {
		maxOutputTokens = strconv.Itoa(*body.MaxOutputTokens)
	}
	return responseSemanticCacheDigest([]string{responseFormat, maxOutputTokens})
}

func responseCapability(body ResponsesRequest) string {
	capabilities := make([]string, 0, len(body.Capabilities)+1)
	if capability := strings.TrimSpace(body.Capability); capability != "" {
		capabilities = append(capabilities, capability)
	}
	for _, capability := range body.Capabilities {
		if capability = strings.TrimSpace(capability); capability != "" {
			capabilities = append(capabilities, capability)
		}
	}
	if len(capabilities) == 0 {
		return "responses.text"
	}
	sort.Strings(capabilities)
	return strings.Join(capabilities, ",")
}

func responseSemanticCacheDigest(parts []string) string {
	encoded, _ := json.Marshal(parts)
	digest := sha256.Sum256(encoded)
	return fmt.Sprintf("resp_cache_%x", digest[:])
}

func responseSemanticCacheEnabled() bool {
	return responseSemanticCacheMode() != ""
}

func responseSemanticCacheMode() string {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(responseSemanticCacheEnv))) {
	case "mock":
		return "mock"
	case "candidate", "deterministic":
		return "candidate"
	case "1", "true", "on", "enabled":
		return "local"
	default:
		return ""
	}
}

func responseSemanticCacheEnabledForWorkspace(workspaceID string, requested *bool) bool {
	if !responseSemanticCacheEnabled() {
		return false
	}
	if requested != nil && !*requested {
		return false
	}
	workspaceID = strings.TrimSpace(workspaceID)
	if workspaceID == "" {
		return false
	}
	for _, item := range strings.Split(os.Getenv(responseSemanticCacheWorkspacesEnv), ",") {
		item = strings.TrimSpace(item)
		if item == workspaceID || item == "*" {
			return true
		}
	}
	return false
}

func responseSemanticCachePGVectorEnabled() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(responseSemanticCachePGVectorEnv))) {
	case "1", "true", "on", "enabled":
		return true
	default:
		return false
	}
}

func responseSemanticCachePGVectorAllowedForWorkspace(workspaceID string) bool {
	if !responseSemanticCachePGVectorEnabled() || !responseSemanticCacheEnabledForWorkspace(workspaceID, nil) {
		return false
	}
	workspaceID = strings.TrimSpace(workspaceID)
	if workspaceID == "" {
		return false
	}
	for _, item := range strings.Split(os.Getenv(responseSemanticCachePGVectorWorkspacesEnv), ",") {
		if strings.TrimSpace(item) == workspaceID {
			return true
		}
	}
	return false
}

func responseSemanticCacheEligible(body ResponsesRequest, route relay.Route) bool {
	if !responseSemanticCacheEnabledForWorkspace(route.WorkspaceID, body.SemanticCache) {
		return false
	}
	if body.Temperature == nil || *body.Temperature != 0 {
		return false
	}
	if len(body.Tools) != 0 || body.ToolChoice != nil {
		return false
	}
	if body.SideEffect || len(body.SideEffects) != 0 {
		return false
	}
	classification := strings.ToUpper(strings.TrimSpace(body.DataClassification))
	if classification != "C0" && classification != "C1" && classification != "C2" {
		return false
	}
	if strings.TrimSpace(body.Region) == "" || strings.TrimSpace(body.GuardPolicyVersion) == "" {
		return false
	}
	return true
}

func responseMockRoute(route relay.Route) bool {
	return strings.EqualFold(firstChannel(route).Provider, "mock")
}

func responseSemanticCacheRouteAllowed(route relay.Route) bool {
	mode := responseSemanticCacheMode()
	return mode == "local" || mode == "candidate" || (mode == "mock" && responseMockRoute(route))
}

func responseSemanticCacheCandidateEnabled() bool {
	if mode := responseSemanticCacheMode(); mode == "candidate" {
		return true
	}
	switch strings.ToLower(strings.TrimSpace(os.Getenv(responseSemanticCacheCandidateEnv))) {
	case "1", "true", "on", "enabled", "candidate", "deterministic", "hash":
		return responseSemanticCacheEnabled()
	default:
		return false
	}
}

func responseSemanticCacheSimilarityThreshold() float64 {
	value, err := strconv.ParseFloat(strings.TrimSpace(os.Getenv(responseSemanticCacheThresholdEnv)), 64)
	if err != nil || value < 0 || value > 1 {
		return responseSemanticCacheDefaultThreshold
	}
	return value
}

func responseSemanticCacheMaxCandidates() int {
	value, err := strconv.Atoi(strings.TrimSpace(os.Getenv(responseSemanticCacheMaxCandidatesEnv)))
	if err != nil || value < 1 || value > responseSemanticCacheMaxSize {
		return responseSemanticCacheDefaultMaxCandidates
	}
	return value
}

type responseSemanticCacheMatch struct {
	entry      responseSemanticCacheEntry
	key        string
	similarity float64
	ok         bool
}

func responseSemanticCacheEntryExpired(entry responseSemanticCacheEntry, now time.Time) bool {
	expiresAt := entry.ExpiresAt
	if expiresAt.IsZero() {
		expiresAt = entry.CreatedAt.Add(responseSemanticCacheTTL)
	}
	return !expiresAt.After(now)
}

func responseSemanticCacheEntryUsable(entry responseSemanticCacheEntry, scope responseSemanticCacheScope, now time.Time, requireEmbedding bool) bool {
	if !responseSemanticCacheScopeEqual(entry.Scope, scope) || strings.TrimSpace(entry.Text) == "" || len(entry.Text) > responseSemanticCacheMaxText || responseSemanticCacheEntryExpired(entry, now) {
		return false
	}
	if requireEmbedding && len(entry.Embedding) != responseSemanticCacheEmbeddingDimensions {
		return false
	}
	return len(entry.Embedding) == 0 || len(entry.Embedding) == responseSemanticCacheEmbeddingDimensions
}

func responseSemanticCacheBetterMatch(left, right responseSemanticCacheMatch) responseSemanticCacheMatch {
	if !left.ok || (right.ok && (right.similarity > left.similarity || (right.similarity == left.similarity && right.key < left.key))) {
		return right
	}
	return left
}

func (s *Server) responseSemanticCacheLookup(record *responseRecord) (responseSemanticCacheEntry, string, float64, bool) {
	if record == nil || record.semanticCacheKey == "" {
		return responseSemanticCacheEntry{}, "", 0, false
	}
	store := s.responseStore()
	store.mu.Lock()
	now := store.now()
	entry, ok := store.semanticCache[record.semanticCacheKey]
	if ok {
		if responseSemanticCacheEntryExpired(entry, now) {
			delete(store.semanticCache, record.semanticCacheKey)
		} else {
			store.mu.Unlock()
			return entry, "exact", 1, true
		}
	}
	localMatch := responseSemanticCacheMatch{}
	candidateEnabled := responseSemanticCacheCandidateEnabled() && len(record.semanticCacheEmbedding) != 0
	threshold := responseSemanticCacheSimilarityThreshold()
	maxCandidates := responseSemanticCacheMaxCandidates()
	if candidateEnabled {
		keys := make([]string, 0, len(store.semanticCache))
		for key := range store.semanticCache {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		checked := 0
		for _, key := range keys {
			candidate := store.semanticCache[key]
			if key == record.semanticCacheKey || !responseSemanticCacheEntryUsable(candidate, record.semanticCacheScope, now, true) {
				if responseSemanticCacheEntryExpired(candidate, now) {
					delete(store.semanticCache, key)
				}
				continue
			}
			checked++
			if checked > maxCandidates {
				break
			}
			similarity := responseSemanticCosine(record.semanticCacheEmbedding, candidate.Embedding)
			if similarity >= threshold {
				localMatch = responseSemanticCacheBetterMatch(localMatch, responseSemanticCacheMatch{entry: candidate, key: key, similarity: similarity, ok: true})
			}
		}
	}
	repository := store.semanticCacheRepository
	store.mu.Unlock()

	persistentMatch := responseSemanticCacheMatch{}
	if repository != nil && record.semanticCacheSafe && responseSemanticCachePGVectorAllowedForWorkspace(record.semanticCacheScope.WorkspaceID) {
		ctx, cancel := context.WithTimeout(context.Background(), responseSemanticCacheRepositoryTimeout)
		result, err := repository.Lookup(ctx, responseSemanticCacheRepositoryQuery{
			Key: record.semanticCacheKey, Scope: record.semanticCacheScope, Embedding: append([]float64(nil), record.semanticCacheEmbedding...),
			Threshold: threshold, MaxCandidates: func() int {
				if candidateEnabled {
					return maxCandidates
				}
				return 0
			}(), Now: now,
		})
		cancel()
		if err != nil {
			store.semanticCacheRepositoryWarnOnce.Do(func() {
				if store.logger != nil {
					store.logger.Warn("response semantic cache repository lookup failed; using in-memory fallback", "error", err)
				}
			})
		} else {
			if result.Exact != nil && responseSemanticCacheEntryUsable(*result.Exact, record.semanticCacheScope, now, false) {
				return *result.Exact, "exact", 1, true
			}
			if candidateEnabled {
				for _, candidate := range result.Candidates {
					if candidate.Key == record.semanticCacheKey || !responseSemanticCacheEntryUsable(candidate.Entry, record.semanticCacheScope, now, true) {
						continue
					}
					similarity := responseSemanticCosine(record.semanticCacheEmbedding, candidate.Entry.Embedding)
					if similarity >= threshold {
						persistentMatch = responseSemanticCacheBetterMatch(persistentMatch, responseSemanticCacheMatch{entry: candidate.Entry, key: candidate.Key, similarity: similarity, ok: true})
					}
				}
			}
		}
	}
	best := responseSemanticCacheBetterMatch(localMatch, persistentMatch)
	if !best.ok {
		return responseSemanticCacheEntry{}, "", 0, false
	}
	return best.entry, "semantic", best.similarity, true
}

func responseSemanticCacheScopeEqual(left, right responseSemanticCacheScope) bool {
	return left == right
}

func (s *Server) responseSemanticCachePut(record *responseRecord, text string, completionTokens int) {
	if record == nil || record.semanticCacheKey == "" || strings.TrimSpace(text) == "" || len(text) > responseSemanticCacheMaxText {
		return
	}
	store := s.responseStore()
	store.mu.Lock()
	if store.semanticCache == nil {
		store.semanticCache = make(map[string]responseSemanticCacheEntry)
	}
	if len(store.semanticCache) >= responseSemanticCacheMaxSize {
		for oldKey := range store.semanticCache {
			delete(store.semanticCache, oldKey)
			break
		}
	}
	embedding := append([]float64(nil), record.semanticCacheEmbedding...)
	entry := responseSemanticCacheEntry{
		Text: text, CompletionTokens: completionTokens, CreatedAt: store.now(), ExpiresAt: store.now().Add(responseSemanticCacheTTL),
		Scope: record.semanticCacheScope, Embedding: embedding,
	}
	store.semanticCache[record.semanticCacheKey] = entry
	repository := store.semanticCacheRepository
	store.mu.Unlock()
	if repository != nil && record.semanticCacheSafe && responseSemanticCachePGVectorAllowedForWorkspace(record.semanticCacheScope.WorkspaceID) {
		ctx, cancel := context.WithTimeout(context.Background(), responseSemanticCacheRepositoryTimeout)
		err := repository.Put(ctx, record.semanticCacheKey, entry)
		cancel()
		if err != nil {
			store.semanticCacheRepositoryWarnOnce.Do(func() {
				if store.logger != nil {
					store.logger.Warn("response semantic cache repository put failed; using in-memory fallback", "error", err)
				}
			})
		}
	}
}

func (s *Server) markResponseCacheHit(record *responseRecord, provenance string, similarity float64) {
	store := s.responseStore()
	store.mu.Lock()
	record.semanticCacheHit = true
	record.semanticCacheProvenance = provenance
	record.semanticCacheSimilarity = similarity
	if record.object.Metadata == nil {
		record.object.Metadata = map[string]string{}
	}
	record.object.Metadata["wama_cache_provenance"] = provenance
	if provenance == "semantic" {
		record.object.Metadata["wama_cache_similarity"] = strconv.FormatFloat(similarity, 'f', 6, 64)
	}
	store.mu.Unlock()
	store.persist(record)
}

func (s *Server) responseCacheWasHit(record *responseRecord) bool {
	store := s.responseStore()
	store.mu.RLock()
	defer store.mu.RUnlock()
	return record.semanticCacheHit
}

func (s *Server) responseCacheHitDetails(record *responseRecord) (string, float64) {
	store := s.responseStore()
	store.mu.RLock()
	defer store.mu.RUnlock()
	return record.semanticCacheProvenance, record.semanticCacheSimilarity
}

func responseSemanticEmbedding(value string) []float64 {
	vector := make([]float64, responseSemanticCacheEmbeddingDimensions)
	for _, token := range responseSemanticEmbeddingTokens(value) {
		digest := sha256.Sum256([]byte(token))
		index := int(binaryLittleEndianUint32(digest[:4]) % responseSemanticCacheEmbeddingDimensions)
		sign := 1.0
		if digest[4]&1 == 1 {
			sign = -1
		}
		vector[index] += sign
	}
	norm := math.Sqrt(responseSemanticDot(vector, vector))
	if norm == 0 {
		return vector
	}
	for index := range vector {
		vector[index] /= norm
	}
	return vector
}

func responseSemanticEmbeddingTokens(value string) []string {
	var builder strings.Builder
	for _, character := range strings.ToLower(value) {
		if unicode.IsLetter(character) || unicode.IsNumber(character) {
			builder.WriteRune(character)
		} else {
			builder.WriteByte(' ')
		}
	}
	fields := strings.Fields(builder.String())
	if len(fields) < 2 {
		return fields
	}
	features := append([]string(nil), fields...)
	for index := 0; index+1 < len(fields); index++ {
		features = append(features, fields[index]+"\x00"+fields[index+1])
	}
	return features
}

func binaryLittleEndianUint32(value []byte) uint32 {
	return uint32(value[0]) | uint32(value[1])<<8 | uint32(value[2])<<16 | uint32(value[3])<<24
}

func responseSemanticDot(left, right []float64) float64 {
	length := len(left)
	if len(right) < length {
		length = len(right)
	}
	var result float64
	for index := 0; index < length; index++ {
		result += left[index] * right[index]
	}
	return result
}

func responseSemanticCosine(left, right []float64) float64 {
	leftNorm := math.Sqrt(responseSemanticDot(left, left))
	rightNorm := math.Sqrt(responseSemanticDot(right, right))
	if leftNorm == 0 || rightNorm == 0 {
		return 0
	}
	return responseSemanticDot(left, right) / (leftNorm * rightNorm)
}

func newResponseID() string {
	return fmt.Sprintf("resp_%x", time.Now().UnixNano())
}

func (s *Server) markResponseInProgress(record *responseRecord) bool {
	store := s.responseStore()
	store.mu.Lock()
	if record.object.Status != "queued" {
		store.mu.Unlock()
		return false
	}
	record.object.Status = "in_progress"
	store.mu.Unlock()
	store.persist(record)
	return true
}

func (s *Server) completeResponse(record *responseRecord, text string, inputTokens, completionTokens int) bool {
	store := s.responseStore()
	store.mu.Lock()
	if record.object.Status == "cancelled" || record.object.Status == "failed" {
		store.mu.Unlock()
		return false
	}
	record.object.Status = "completed"
	record.object.OutputText = text
	record.object.Output = []responseOutputItem{{
		ID: "msg_" + strings.TrimPrefix(record.object.ID, "resp_"), Type: "message", Status: "completed", Role: "assistant",
		Content: []responseOutputBlock{{Type: "output_text", Text: text, Annotations: []any{}}},
	}}
	record.object.Usage = &responseUsage{InputTokens: inputTokens, OutputTokens: completionTokens, TotalTokens: inputTokens + completionTokens}
	store.mu.Unlock()
	store.persist(record)
	return true
}

func (s *Server) failResponse(record *responseRecord, code, message string) bool {
	store := s.responseStore()
	store.mu.Lock()
	if record.object.Status == "cancelled" || record.object.Status == "completed" {
		store.mu.Unlock()
		return false
	}
	record.object.Status = "failed"
	record.object.Error = &responseError{Type: "api_error", Code: code, Message: message}
	record.object.IncompleteDetails = &responseIncompleteDetail{Reason: "error"}
	store.mu.Unlock()
	store.persist(record)
	return true
}

func (s *Server) cancelResponseRecord(record *responseRecord) bool {
	store := s.responseStore()
	store.mu.Lock()
	if record.object.Status == "cancelled" {
		store.mu.Unlock()
		return true
	}
	if record.object.Status != "queued" && record.object.Status != "in_progress" {
		store.mu.Unlock()
		return false
	}
	record.object.Status = "cancelled"
	record.object.IncompleteDetails = &responseIncompleteDetail{Reason: "cancelled"}
	cancel := record.cancel
	store.mu.Unlock()
	store.persist(record)
	if cancel != nil {
		cancel()
	}
	s.releaseResponseRecord(record, context.Background())
	return true
}

func (s *Server) releaseResponseReservation(requestID string, ctx context.Context) {
	if s.Platform == nil || requestID == "" {
		return
	}
	if err := s.Platform.Release(context.WithoutCancel(ctx), requestID); err != nil && s.Logger != nil {
		s.Logger.Error("response budget release failed", "request_id", requestID, "error", err)
	}
}

func (s *Server) releaseResponseRecord(record *responseRecord, ctx context.Context) {
	record.releaseOnce.Do(func() {
		s.releaseResponseReservation(record.requestID, ctx)
	})
}

func (s *Server) meterResponse(record *responseRecord, channel relay.Channel, promptTokens, completionTokens, statusCode int) {
	if s.Metering == nil {
		return
	}
	record.meterOnce.Do(func() {
		s.recordMeter(context.Background(), meteringRecord(record, channel, promptTokens, completionTokens, statusCode))
	})
}

func meteringRecord(record *responseRecord, channel relay.Channel, promptTokens, completionTokens, statusCode int) metering.Record {
	return metering.Record{
		RequestID: record.requestID, WorkspaceID: record.workspaceID, ChannelID: channel.ID,
		Model: record.model, PromptTokens: promptTokens, CompletionTokens: completionTokens,
		LatencyMS: 0, StatusCode: statusCode,
	}
}

func (s *Server) responseSnapshot(record *responseRecord) responseObject {
	store := s.responseStore()
	store.mu.RLock()
	defer store.mu.RUnlock()
	return cloneResponseObject(record.object)
}

func cloneResponseObject(value responseObject) responseObject {
	copyValue := value
	copyValue.Output = make([]responseOutputItem, len(value.Output))
	for index, item := range value.Output {
		copyItem := item
		copyItem.Content = append([]responseOutputBlock(nil), item.Content...)
		copyValue.Output[index] = copyItem
	}
	if value.Error != nil {
		errorValue := *value.Error
		copyValue.Error = &errorValue
	}
	if value.IncompleteDetails != nil {
		detail := *value.IncompleteDetails
		copyValue.IncompleteDetails = &detail
	}
	if value.Usage != nil {
		usage := *value.Usage
		copyValue.Usage = &usage
	}
	copyValue.Metadata = make(map[string]string, len(value.Metadata))
	for key, item := range value.Metadata {
		copyValue.Metadata[key] = item
	}
	return copyValue
}

func (store *responseRegistry) get(responseID string) (*responseRecord, bool) {
	store.mu.Lock()
	record, ok := store.records[responseID]
	if ok && !record.expiresAt.IsZero() && !record.expiresAt.After(store.now()) {
		delete(store.records, responseID)
		store.mu.Unlock()
		store.remove(responseID)
		return nil, false
	}
	store.mu.Unlock()
	return record, ok
}


func (s *Server) recoverBackgroundResponses() {
	store := s.responseStore()
	store.mu.Lock()
	var toRecover []*responseRecord
	for _, record := range store.records {
		if record.background && record.object.Status == "queued" && record.chatBody.Model != "" {
			toRecover = append(toRecover, record)
		}
	}
	store.mu.Unlock()
	for _, record := range toRecover {
		route, err := s.Platform.Resolve(context.Background(), "", record.workspaceID, record.model)
		if err != nil {
			s.failResponse(record, "recovery_failed", "Could not resolve route after gateway restart")
			continue
		}
		ctx, cancel := context.WithCancel(context.Background())
		record.cancel = cancel
		go s.runBackgroundResponse(ctx, record, route, record.chatBody, record.promptTokens)
	}
}
