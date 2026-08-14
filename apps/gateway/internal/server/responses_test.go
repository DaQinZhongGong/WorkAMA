package server

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/workama/workama/apps/gateway/internal/relay"
)

type responseSemanticCacheRepositoryStub struct {
	mu        sync.Mutex
	entries   map[string]responseSemanticCacheEntry
	lookupErr error
	putErr    error
	lookups   int
	puts      int
}

type responseSemanticCacheSQLRowsStub struct {
	rows    [][]any
	current []any
	index   int
	closed  bool
}

func (rows *responseSemanticCacheSQLRowsStub) Next() bool {
	if rows.index >= len(rows.rows) {
		return false
	}
	rows.current = rows.rows[rows.index]
	rows.index++
	return true
}

func (rows *responseSemanticCacheSQLRowsStub) Scan(dest ...any) error {
	if len(dest) != len(rows.current) {
		return errors.New("sql test row column count mismatch")
	}
	for index, value := range rows.current {
		switch target := dest[index].(type) {
		case *string:
			text, ok := value.(string)
			if !ok {
				return errors.New("sql test row string type mismatch")
			}
			*target = text
		case *int:
			integer, ok := value.(int)
			if !ok {
				return errors.New("sql test row int type mismatch")
			}
			*target = integer
		case *float64:
			float, ok := value.(float64)
			if !ok {
				return errors.New("sql test row float type mismatch")
			}
			*target = float
		case *time.Time:
			timestamp, ok := value.(time.Time)
			if !ok {
				return errors.New("sql test row timestamp type mismatch")
			}
			*target = timestamp
		default:
			return errors.New("sql test row destination type unsupported")
		}
	}
	return nil
}

func (rows *responseSemanticCacheSQLRowsStub) Close() error {
	rows.closed = true
	return nil
}

func (rows *responseSemanticCacheSQLRowsStub) Err() error { return nil }

type responseSemanticCacheSQLExecutorStub struct {
	exactRows     [][]any
	candidateRows [][]any
	queries       []string
	args          [][]any
	execQuery     string
	execArgs      []any
	queryErr      error
	execErr       error
}

func (executor *responseSemanticCacheSQLExecutorStub) QueryContext(_ context.Context, query string, args ...any) (ResponseSemanticCacheSQLRows, error) {
	executor.queries = append(executor.queries, query)
	executor.args = append(executor.args, append([]any(nil), args...))
	if executor.queryErr != nil {
		return nil, executor.queryErr
	}
	rows := executor.exactRows
	if !strings.Contains(query, "cache_key = $1") {
		rows = executor.candidateRows
	}
	return &responseSemanticCacheSQLRowsStub{rows: rows}, nil
}

func (executor *responseSemanticCacheSQLExecutorStub) ExecContext(_ context.Context, query string, args ...any) error {
	executor.execQuery = query
	executor.execArgs = append([]any(nil), args...)
	return executor.execErr
}

type blockingResponseSemanticCacheRepository struct{}

func (blockingResponseSemanticCacheRepository) Lookup(ctx context.Context, _ ResponseSemanticCacheLookupRequest) (ResponseSemanticCacheLookupResult, error) {
	<-ctx.Done()
	return ResponseSemanticCacheLookupResult{}, ctx.Err()
}

func (blockingResponseSemanticCacheRepository) Put(context.Context, string, ResponseSemanticCacheEntry) error {
	return nil
}

type closableResponseSemanticCacheRepository struct {
	closed int
}

func (repository *closableResponseSemanticCacheRepository) Lookup(context.Context, ResponseSemanticCacheLookupRequest) (ResponseSemanticCacheLookupResult, error) {
	return ResponseSemanticCacheLookupResult{}, nil
}

func (repository *closableResponseSemanticCacheRepository) Put(context.Context, string, ResponseSemanticCacheEntry) error {
	return nil
}

func (repository *closableResponseSemanticCacheRepository) Close() error {
	repository.closed++
	return nil
}

func (stub *responseSemanticCacheRepositoryStub) Lookup(_ context.Context, query ResponseSemanticCacheLookupRequest) (ResponseSemanticCacheLookupResult, error) {
	stub.mu.Lock()
	defer stub.mu.Unlock()
	stub.lookups++
	if stub.lookupErr != nil {
		return ResponseSemanticCacheLookupResult{}, stub.lookupErr
	}
	result := ResponseSemanticCacheLookupResult{}
	for key, entry := range stub.entries {
		if !responseSemanticCacheScopeEqual(entry.Scope, query.Scope) || responseSemanticCacheEntryExpired(entry, query.Now) {
			continue
		}
		if key == query.Key {
			copyEntry := entry
			result.Exact = &copyEntry
			continue
		}
		if query.MaxCandidates > 0 && len(entry.Embedding) == len(query.Embedding) {
			result.Candidates = append(result.Candidates, ResponseSemanticCacheCandidate{Key: key, Entry: entry, Similarity: responseSemanticCosine(query.Embedding, entry.Embedding)})
		}
	}
	return result, nil
}

func (stub *responseSemanticCacheRepositoryStub) Put(_ context.Context, key string, entry ResponseSemanticCacheEntry) error {
	stub.mu.Lock()
	defer stub.mu.Unlock()
	stub.puts++
	if stub.putErr != nil {
		return stub.putErr
	}
	if stub.entries == nil {
		stub.entries = make(map[string]responseSemanticCacheEntry)
	}
	stub.entries[key] = entry
	return nil
}

type responseControlStub struct {
	mu          sync.Mutex
	calls       []string
	rateAllowed bool
	releases    int
}

func newResponseTestServer(t *testing.T) (*Server, http.Handler, *responseControlStub) {
	t.Helper()
	control := &responseControlStub{rateAllowed: true}
	platform := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		control.mu.Lock()
		control.calls = append(control.calls, r.URL.Path)
		rateAllowed := control.rateAllowed
		control.mu.Unlock()
		switch r.URL.Path {
		case "/internal/gateway/resolve":
			workspaceID := "ws_alpha"
			var resolveRequest struct {
				APIKey string `json:"api_key"`
			}
			_ = json.NewDecoder(r.Body).Decode(&resolveRequest)
			if resolveRequest.APIKey == "other-key" {
				workspaceID = "ws_other"
			}
			writeJSON(w, http.StatusOK, map[string]any{
				"workspace_id": workspaceID, "rpm_limit": 60, "tpm_limit": 100000,
				"channels": []map[string]any{{"id": "chn_mock", "provider": "mock", "base_url": "mock://local", "api_key": "provider-secret"}},
			})
		case "/internal/gateway/rate-limit/batch":
			if !rateAllowed {
				writeJSON(w, http.StatusOK, map[string]any{"allowed": false, "retry_after": 3})
				return
			}
			writeJSON(w, http.StatusOK, map[string]any{"allowed": true, "rpm_used": 1, "tpm_used": 4})
		case "/internal/gateway/reserve":
			writeJSON(w, http.StatusOK, map[string]any{"reservation_id": "res_response", "estimated_cost": 1, "status": "active"})
		case "/internal/security/moderate":
			var request struct {
				Text string `json:"text"`
			}
			_ = json.NewDecoder(r.Body).Decode(&request)
			writeJSON(w, http.StatusOK, map[string]any{"action": "allow", "text": request.Text})
		case "/internal/gateway/release":
			control.mu.Lock()
			control.releases++
			control.mu.Unlock()
			w.WriteHeader(http.StatusNoContent)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(platform.Close)
	server := New(relay.NewPlatformClient(platform.URL, "internal"), nil, "internal", slog.New(slog.NewTextHandler(io.Discard, nil)))
	return server, server.Handler(), control
}

func TestResponsesSyncMockReturnsResponseItemsAndOutputText(t *testing.T) {
	server, handler, control := newResponseTestServer(t)
	request := httptest.NewRequest(http.MethodPost, "/v1/responses", strings.NewReader(`{"model":"workama-chat","input":"hello response"}`))
	request.Header.Set("Authorization", "Bearer alpha-key")
	created := httptest.NewRecorder()
	handler.ServeHTTP(created, request)
	if created.Code != http.StatusOK {
		t.Fatalf("create status = %d, body = %s", created.Code, created.Body.String())
	}
	if strings.Contains(created.Body.String(), "provider-secret") {
		t.Fatal("provider credential leaked in response body")
	}
	var response responseObject
	if err := json.Unmarshal(created.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Object != "response" || response.Status != "completed" || response.OutputText == "" {
		t.Fatalf("response shape = %#v", response)
	}
	if len(response.Output) != 1 || response.Output[0].Type != "message" || len(response.Output[0].Content) != 1 || response.Output[0].Content[0].Type != "output_text" {
		t.Fatalf("response output items = %#v", response.Output)
	}

	poll := httptest.NewRequest(http.MethodGet, "/v1/responses/"+response.ID, nil)
	poll.Header.Set("Authorization", "Bearer alpha-key")
	polled := httptest.NewRecorder()
	handler.ServeHTTP(polled, poll)
	var polledResponse responseObject
	if json.Unmarshal(polled.Body.Bytes(), &polledResponse) != nil || polled.Code != http.StatusOK || polledResponse.OutputText != response.OutputText {
		t.Fatalf("poll status = %d, body = %s", polled.Code, polled.Body.String())
	}
	control.mu.Lock()
	calls := append([]string(nil), control.calls...)
	control.mu.Unlock()
	if len(calls) < 6 || calls[0] != "/internal/gateway/resolve" || calls[1] != "/internal/gateway/rate-limit/batch" || calls[2] != "/internal/gateway/reserve" {
		t.Fatalf("pipeline calls = %#v", calls)
	}
	control.mu.Lock()
	releases := control.releases
	control.mu.Unlock()
	if releases != 0 {
		t.Fatalf("successful response released budget %d times", releases)
	}
	_ = server
}

func TestResponsesStreamingEmitsCreatedDeltaAndCompletedEvents(t *testing.T) {
	_, handler, _ := newResponseTestServer(t)
	request := httptest.NewRequest(http.MethodPost, "/v1/responses", strings.NewReader(`{"model":"workama-chat","input":"stream response","stream":true}`))
	request.Header.Set("Authorization", "Bearer alpha-key")
	created := httptest.NewRecorder()
	handler.ServeHTTP(created, request)
	if created.Code != http.StatusOK || !strings.HasPrefix(created.Header().Get("Content-Type"), "text/event-stream") {
		t.Fatalf("stream response = %d %s %s", created.Code, created.Header().Get("Content-Type"), created.Body.String())
	}
	body := created.Body.String()
	for _, marker := range []string{"event: response.created", "event: response.output_text.delta", "event: response.completed", "data: [DONE]", "stream response"} {
		if !strings.Contains(body, marker) {
			t.Fatalf("stream body missing %q: %s", marker, body)
		}
	}
}

func TestResponsesRejectBackgroundStreamingCombination(t *testing.T) {
	_, handler, _ := newResponseTestServer(t)
	request := httptest.NewRequest(http.MethodPost, "/v1/responses", strings.NewReader(`{"model":"workama-chat","input":"invalid","stream":true,"background":true}`))
	request.Header.Set("Authorization", "Bearer alpha-key")
	result := httptest.NewRecorder()
	handler.ServeHTTP(result, request)
	if result.Code != http.StatusBadRequest || !strings.Contains(result.Body.String(), "background Responses") {
		t.Fatalf("invalid stream/background response = %d %s", result.Code, result.Body.String())
	}
}

func TestResponsesBackgroundCanBePolledCancelledAndTenantScoped(t *testing.T) {
	_, handler, control := newResponseTestServer(t)
	request := httptest.NewRequest(http.MethodPost, "/v1/responses", strings.NewReader(`{"model":"workama-chat","input":"long task","background":true}`))
	request.Header.Set("Authorization", "Bearer alpha-key")
	created := httptest.NewRecorder()
	handler.ServeHTTP(created, request)
	if created.Code != http.StatusAccepted {
		t.Fatalf("background create status = %d, body = %s", created.Code, created.Body.String())
	}
	var response responseObject
	if err := json.Unmarshal(created.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Status != "queued" {
		t.Fatalf("initial background status = %q", response.Status)
	}

	otherTenant := httptest.NewRequest(http.MethodGet, "/v1/responses/"+response.ID, nil)
	otherTenant.Header.Set("Authorization", "Bearer other-key")
	otherResult := httptest.NewRecorder()
	handler.ServeHTTP(otherResult, otherTenant)
	if otherResult.Code != http.StatusNotFound {
		t.Fatalf("cross-tenant poll status = %d, body = %s", otherResult.Code, otherResult.Body.String())
	}

	cancel := httptest.NewRequest(http.MethodPost, "/v1/responses/"+response.ID+"/cancel", nil)
	cancel.Header.Set("Authorization", "Bearer alpha-key")
	cancelResult := httptest.NewRecorder()
	handler.ServeHTTP(cancelResult, cancel)
	if cancelResult.Code != http.StatusOK || !strings.Contains(cancelResult.Body.String(), `"status":"cancelled"`) {
		t.Fatalf("cancel status = %d, body = %s", cancelResult.Code, cancelResult.Body.String())
	}
	time.Sleep(40 * time.Millisecond)
	poll := httptest.NewRequest(http.MethodGet, "/v1/responses/"+response.ID, nil)
	poll.Header.Set("Authorization", "Bearer alpha-key")
	polled := httptest.NewRecorder()
	handler.ServeHTTP(polled, poll)
	if polled.Code != http.StatusOK || !strings.Contains(polled.Body.String(), `"status":"cancelled"`) {
		t.Fatalf("cancelled poll status = %d, body = %s", polled.Code, polled.Body.String())
	}
	control.mu.Lock()
	releases := control.releases
	control.mu.Unlock()
	if releases != 1 {
		t.Fatalf("cancelled response released budget %d times", releases)
	}
}

func TestResponsesBackgroundEventuallyCompletes(t *testing.T) {
	_, handler, _ := newResponseTestServer(t)
	request := httptest.NewRequest(http.MethodPost, "/v1/responses", strings.NewReader(`{"model":"workama-chat","input":"poll me","background":true}`))
	request.Header.Set("Authorization", "Bearer alpha-key")
	created := httptest.NewRecorder()
	handler.ServeHTTP(created, request)
	var response responseObject
	if err := json.Unmarshal(created.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		poll := httptest.NewRequest(http.MethodGet, "/v1/responses/"+response.ID, nil)
		poll.Header.Set("Authorization", "Bearer alpha-key")
		polled := httptest.NewRecorder()
		handler.ServeHTTP(polled, poll)
		var current responseObject
		if err := json.Unmarshal(polled.Body.Bytes(), &current); err != nil {
			t.Fatal(err)
		}
		if current.Status == "completed" {
			if current.OutputText == "" || current.Usage == nil || current.Usage.TotalTokens == 0 {
				t.Fatalf("completed response missing output/usage: %#v", current)
			}
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("background response did not complete")
}

func TestResponsesRateLimitRunsBeforeBudgetReservation(t *testing.T) {
	_, handler, control := newResponseTestServer(t)
	control.mu.Lock()
	control.rateAllowed = false
	control.mu.Unlock()
	request := httptest.NewRequest(http.MethodPost, "/v1/responses", strings.NewReader(`{"model":"workama-chat","input":"limited"}`))
	request.Header.Set("Authorization", "Bearer alpha-key")
	result := httptest.NewRecorder()
	handler.ServeHTTP(result, request)
	if result.Code != http.StatusTooManyRequests {
		t.Fatalf("rate limit status = %d, body = %s", result.Code, result.Body.String())
	}
	control.mu.Lock()
	calls := append([]string(nil), control.calls...)
	control.mu.Unlock()
	for _, call := range calls {
		if call == "/internal/gateway/reserve" {
			t.Fatalf("budget reservation happened after rate limit denial: %#v", calls)
		}
	}
}

func TestResponseInputTextParsesStableTextForms(t *testing.T) {
	tests := []struct {
		name  string
		input any
		want  string
	}{
		{name: "string", input: " hello ", want: "hello"},
		{name: "input item array", input: []any{"first", map[string]any{"type": "input_text", "text": "second"}}, want: "first\nsecond"},
		{name: "message content array", input: map[string]any{"type": "message", "role": "user", "content": []any{map[string]any{"type": "input_text", "text": "first"}, map[string]any{"type": "text", "text": "second"}}}, want: "first\nsecond"},
		{name: "mixed fields have stable order", input: map[string]any{"content": "third", "input_text": "second", "text": "first"}, want: "first\nsecond\nthird"},
		{name: "nested content", input: map[string]any{"content": []any{"outer", map[string]any{"content": []any{"inner"}}}}, want: "outer\ninner"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got, err := responseInputText(test.input)
			if err != nil || got != test.want {
				t.Fatalf("responseInputText() = %q, %v; want %q", got, err, test.want)
			}
		})
	}
}

func TestResponsesRejectMultimodalAndUnknownInputWithoutEchoingPayload(t *testing.T) {
	tests := []struct {
		name string
		body string
	}{
		{name: "image", body: `{"model":"workama-chat","input":[{"type":"input_image","image_url":{"url":"data:image/png;base64,RAW_BINARY_SENTINEL","api_key":"provider-secret"}}]}`},
		{name: "audio", body: `{"model":"workama-chat","input":[{"type":"input_audio","input_audio":{"data":"RAW_AUDIO_SENTINEL","api_key":"provider-secret"}}]}`},
		{name: "unknown object", body: `{"model":"workama-chat","input":[{"type":"function_call","arguments":"credential-secret"}]}`},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			_, handler, control := newResponseTestServer(t)
			request := httptest.NewRequest(http.MethodPost, "/v1/responses", strings.NewReader(test.body))
			request.Header.Set("Authorization", "Bearer alpha-key")
			result := httptest.NewRecorder()
			handler.ServeHTTP(result, request)
			if result.Code != http.StatusBadRequest {
				t.Fatalf("status = %d, body = %s", result.Code, result.Body.String())
			}
			for _, sentinel := range []string{"RAW_BINARY_SENTINEL", "RAW_AUDIO_SENTINEL", "provider-secret", "credential-secret"} {
				if strings.Contains(result.Body.String(), sentinel) {
					t.Fatalf("unsafe payload %q leaked in error body: %s", sentinel, result.Body.String())
				}
			}
			control.mu.Lock()
			calls := append([]string(nil), control.calls...)
			control.mu.Unlock()
			if len(calls) != 0 {
				t.Fatalf("unsafe input reached platform pipeline: %#v", calls)
			}
		})
	}
}

func TestResponsesMetadataAndPreviousResponseAreTenantScoped(t *testing.T) {
	_, handler, _ := newResponseTestServer(t)
	firstRequest := httptest.NewRequest(http.MethodPost, "/v1/responses", strings.NewReader(`{"model":"workama-chat","input":"first response","metadata":{"trace_id":"trace-123"}}`))
	firstRequest.Header.Set("Authorization", "Bearer alpha-key")
	firstResult := httptest.NewRecorder()
	handler.ServeHTTP(firstResult, firstRequest)
	if firstResult.Code != http.StatusOK {
		t.Fatalf("first response status = %d, body = %s", firstResult.Code, firstResult.Body.String())
	}
	var first responseObject
	if err := json.Unmarshal(firstResult.Body.Bytes(), &first); err != nil {
		t.Fatal(err)
	}
	if first.Metadata["trace_id"] != "trace-123" {
		t.Fatalf("metadata = %#v", first.Metadata)
	}

	followUpBody, err := json.Marshal(map[string]any{
		"model":                "workama-chat",
		"input":                []any{map[string]any{"content": []any{map[string]any{"type": "input_text", "text": "follow up"}}}},
		"previous_response_id": first.ID,
	})
	if err != nil {
		t.Fatal(err)
	}
	followUpRequest := httptest.NewRequest(http.MethodPost, "/v1/responses", strings.NewReader(string(followUpBody)))
	followUpRequest.Header.Set("Authorization", "Bearer alpha-key")
	followUpResult := httptest.NewRecorder()
	handler.ServeHTTP(followUpResult, followUpRequest)
	if followUpResult.Code != http.StatusOK {
		t.Fatalf("follow-up status = %d, body = %s", followUpResult.Code, followUpResult.Body.String())
	}
	var followUp responseObject
	if err := json.Unmarshal(followUpResult.Body.Bytes(), &followUp); err != nil {
		t.Fatal(err)
	}
	if followUp.PreviousResponseID != first.ID {
		t.Fatalf("previous_response_id = %q, want %q", followUp.PreviousResponseID, first.ID)
	}
	chat := responseChatRequestWithPrevious(ResponsesRequest{Model: "workama-chat"}, "follow up", first.OutputText)
	if len(chat.Messages) != 2 || chat.Messages[0].Role != "assistant" || chat.Messages[0].Content != first.OutputText || chat.Messages[1].Role != "user" {
		t.Fatalf("previous response context = %#v", chat.Messages)
	}

	crossTenantBody, err := json.Marshal(map[string]any{"model": "workama-chat", "input": "cross tenant", "previous_response_id": first.ID})
	if err != nil {
		t.Fatal(err)
	}
	crossTenantRequest := httptest.NewRequest(http.MethodPost, "/v1/responses", strings.NewReader(string(crossTenantBody)))
	crossTenantRequest.Header.Set("Authorization", "Bearer other-key")
	crossTenantResult := httptest.NewRecorder()
	handler.ServeHTTP(crossTenantResult, crossTenantRequest)
	if crossTenantResult.Code != http.StatusNotFound || strings.Contains(crossTenantResult.Body.String(), first.OutputText) {
		t.Fatalf("cross-tenant previous response status = %d, body = %s", crossTenantResult.Code, crossTenantResult.Body.String())
	}

	invalidMetadata := httptest.NewRequest(http.MethodPost, "/v1/responses", strings.NewReader(`{"model":"workama-chat","input":"metadata","metadata":{"api_key":"provider-secret"}}`))
	invalidMetadata.Header.Set("Authorization", "Bearer alpha-key")
	invalidMetadataResult := httptest.NewRecorder()
	handler.ServeHTTP(invalidMetadataResult, invalidMetadata)
	if invalidMetadataResult.Code != http.StatusBadRequest || strings.Contains(invalidMetadataResult.Body.String(), "provider-secret") {
		t.Fatalf("restricted metadata status = %d, body = %s", invalidMetadataResult.Code, invalidMetadataResult.Body.String())
	}
}

func TestResponsesSemanticCacheIsOptInAndScopedByTenantAndModel(t *testing.T) {
	t.Setenv(responseSemanticCacheEnv, "mock")
	t.Setenv(responseSemanticCacheWorkspacesEnv, "ws_alpha,ws_other")
	server, handler, _ := newResponseTestServer(t)

	create := func(apiKey, model, input string) (responseObject, string) {
		t.Helper()
		body, err := json.Marshal(map[string]any{
			"model": model, "input": input, "temperature": 0,
			"region": "global", "guard_policy_version": "guard-v1", "data_classification": "C2",
			"semantic_cache": true,
		})
		if err != nil {
			t.Fatal(err)
		}
		request := httptest.NewRequest(http.MethodPost, "/v1/responses", strings.NewReader(string(body)))
		request.Header.Set("Authorization", "Bearer "+apiKey)
		result := httptest.NewRecorder()
		handler.ServeHTTP(result, request)
		if result.Code != http.StatusOK {
			t.Fatalf("create status = %d, body = %s", result.Code, result.Body.String())
		}
		var response responseObject
		if err := json.Unmarshal(result.Body.Bytes(), &response); err != nil {
			t.Fatal(err)
		}
		return response, result.Header().Get("x-wama-cache")
	}

	first, _ := create("alpha-key", "workama-chat", "Cache First")
	second, secondCacheHeader := create("alpha-key", "workama-chat", "cache   first")
	if second.OutputText != first.OutputText {
		t.Fatalf("normalized cache did not reuse output: first=%q second=%q", first.OutputText, second.OutputText)
	}
	if secondCacheHeader != "hit" {
		t.Fatalf("semantic cache hit header = %q, want hit", secondCacheHeader)
	}
	otherTenant, _ := create("other-key", "workama-chat", "cache first")
	otherModel, _ := create("alpha-key", "other-model", "cache first")
	if otherTenant.OutputText == first.OutputText || otherModel.OutputText == first.OutputText {
		t.Fatalf("tenant/model isolation failed: tenant=%q model=%q first=%q", otherTenant.OutputText, otherModel.OutputText, first.OutputText)
	}

	store := server.responseStore()
	store.mu.RLock()
	cacheSize := len(store.semanticCache)
	store.mu.RUnlock()
	if cacheSize != 3 {
		t.Fatalf("semantic cache size = %d, want 3 isolated entries", cacheSize)
	}
	if responseSemanticCacheKey("ws_alpha", "workama-chat", "same", "", 0) == responseSemanticCacheKey("ws_other", "workama-chat", "same", "", 0) || responseSemanticCacheKey("ws_alpha", "workama-chat", "same", "", 0) == responseSemanticCacheKey("ws_alpha", "other-model", "same", "", 0) {
		t.Fatal("semantic cache key is not tenant/model scoped")
	}
}

func TestResponsesSemanticCandidateUsesDeterministicCosineAndReportsProvenance(t *testing.T) {
	t.Setenv(responseSemanticCacheEnv, "candidate")
	t.Setenv(responseSemanticCacheWorkspacesEnv, "ws_alpha")
	server, handler, _ := newResponseTestServer(t)

	create := func(input string) (responseObject, *httptest.ResponseRecorder) {
		t.Helper()
		body := `{"model":"workama-chat","input":"` + input + `","temperature":0,"region":"global","guard_policy_version":"guard-v1","data_classification":"C2","semantic_cache":true}`
		request := httptest.NewRequest(http.MethodPost, "/v1/responses", strings.NewReader(body))
		request.Header.Set("Authorization", "Bearer alpha-key")
		result := httptest.NewRecorder()
		handler.ServeHTTP(result, request)
		if result.Code != http.StatusOK {
			t.Fatalf("create status = %d, body = %s", result.Code, result.Body.String())
		}
		var response responseObject
		if err := json.Unmarshal(result.Body.Bytes(), &response); err != nil {
			t.Fatal(err)
		}
		return response, result
	}

	first, firstResult := create("Semantic cache candidate")
	second, secondResult := create("Semantic cache candidate!")
	if first.OutputText != second.OutputText {
		t.Fatalf("candidate did not reuse output: first=%q second=%q", first.OutputText, second.OutputText)
	}
	if secondResult.Header().Get("x-wama-cache") != "hit" || secondResult.Header().Get("x-wama-cache-provenance") != "semantic" {
		t.Fatalf("candidate headers = cache=%q provenance=%q", secondResult.Header().Get("x-wama-cache"), secondResult.Header().Get("x-wama-cache-provenance"))
	}
	if secondResult.Header().Get("x-wama-cache-similarity") == "" || second.Metadata["wama_cache_provenance"] != "semantic" {
		t.Fatalf("candidate provenance missing: headers=%q metadata=%#v", secondResult.Header().Get("x-wama-cache-similarity"), second.Metadata)
	}
	if firstResult.Header().Get("x-wama-cache") != "" {
		t.Fatalf("first request unexpectedly hit cache: %q", firstResult.Header().Get("x-wama-cache"))
	}

	store := server.responseStore()
	store.mu.RLock()
	cacheSize := len(store.semanticCache)
	store.mu.RUnlock()
	if cacheSize != 1 {
		t.Fatalf("semantic candidate created %d entries, want 1", cacheSize)
	}
}

func TestResponsesSemanticCacheRequiresSafeWorkspaceContext(t *testing.T) {
	t.Setenv(responseSemanticCacheEnv, "mock")
	t.Setenv(responseSemanticCacheWorkspacesEnv, "ws_alpha")
	zero := 0.0
	route := relay.Route{
		WorkspaceID: "ws_alpha",
		TokenID:     stringPointer("token_alpha"),
		GroupID:     stringPointer("group_alpha"),
		Channel:     relay.Channel{ID: "chn_mock", Provider: "mock", BaseURL: "mock://local", UpstreamModel: "workama-chat"},
	}
	base := ResponsesRequest{
		Model: "workama-chat", Temperature: &zero, Region: "global",
		GuardPolicyVersion: "guard-v1", DataClassification: "C2", SemanticCache: boolPointer(true),
	}
	if !responseSemanticCacheEligible(base, route) {
		t.Fatal("safe explicitly enabled request was rejected")
	}
	unsafeCases := []ResponsesRequest{
		base,
		{Model: base.Model, Region: base.Region, GuardPolicyVersion: base.GuardPolicyVersion, DataClassification: base.DataClassification, SemanticCache: base.SemanticCache},
		{Model: base.Model, Temperature: floatPointer(0.2), Region: base.Region, GuardPolicyVersion: base.GuardPolicyVersion, DataClassification: base.DataClassification, SemanticCache: base.SemanticCache},
		{Model: base.Model, Temperature: &zero, Tools: []any{"file.write"}, Region: base.Region, GuardPolicyVersion: base.GuardPolicyVersion, DataClassification: base.DataClassification, SemanticCache: base.SemanticCache},
		{Model: base.Model, Temperature: &zero, Region: base.Region, GuardPolicyVersion: base.GuardPolicyVersion, DataClassification: "C3", SemanticCache: base.SemanticCache},
		{Model: base.Model, Temperature: &zero, Region: base.Region, GuardPolicyVersion: base.GuardPolicyVersion, DataClassification: "C2", SideEffect: true, SemanticCache: base.SemanticCache},
		{Model: base.Model, Temperature: &zero, Region: base.Region, GuardPolicyVersion: base.GuardPolicyVersion, DataClassification: "C2", SideEffects: []any{"write"}, SemanticCache: base.SemanticCache},
	}
	for index, candidate := range unsafeCases[1:] {
		if responseSemanticCacheEligible(candidate, route) {
			t.Fatalf("unsafe cache case %d was eligible", index)
		}
	}
	if responseSemanticCacheEligible(base, relay.Route{WorkspaceID: "ws_other", Channel: route.Channel}) {
		t.Fatal("workspace allowlist did not isolate semantic cache")
	}
	if responseSemanticCacheKeyForRequest(route, base, "same", "prompt", 1) == responseSemanticCacheKeyForRequest(route, base, "same", "prompt", 2) {
		t.Fatal("prompt version was not included in semantic cache key")
	}
	otherRegion := base
	otherRegion.Region = "eu"
	if responseSemanticCacheKeyForRequest(route, base, "same", "prompt", 1) == responseSemanticCacheKeyForRequest(route, otherRegion, "same", "prompt", 1) {
		t.Fatal("region was not included in semantic cache key")
	}
	withCapability := base
	withCapability.Capability = "responses.json"
	if responseSemanticCacheKeyForRequest(route, base, "same", "prompt", 1) == responseSemanticCacheKeyForRequest(route, withCapability, "same", "prompt", 1) {
		t.Fatal("capability was not included in semantic cache key")
	}
	if responseSemanticCacheKeyForRequestWithPromptChecksum(route, base, "same", "prompt", 1, "checksum-a") == responseSemanticCacheKeyForRequestWithPromptChecksum(route, base, "same", "prompt", 1, "checksum-b") {
		t.Fatal("prompt checksum was not included in semantic cache key")
	}
	if got := responseSemanticCacheSimilarityThreshold(); got != responseSemanticCacheDefaultThreshold {
		t.Fatalf("default semantic threshold = %v, want %v", got, responseSemanticCacheDefaultThreshold)
	}
}

func stringPointer(value string) *string { return &value }

func boolPointer(value bool) *bool { return &value }

func floatPointer(value float64) *float64 { return &value }

func TestResponsesSemanticCacheDisabledByDefault(t *testing.T) {
	t.Setenv(responseSemanticCacheEnv, "")
	server, handler, _ := newResponseTestServer(t)
	request := httptest.NewRequest(http.MethodPost, "/v1/responses", strings.NewReader(`{"model":"workama-chat","input":"no cache"}`))
	request.Header.Set("Authorization", "Bearer alpha-key")
	result := httptest.NewRecorder()
	handler.ServeHTTP(result, request)
	if result.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", result.Code, result.Body.String())
	}
	store := server.responseStore()
	store.mu.RLock()
	cacheSize := len(store.semanticCache)
	store.mu.RUnlock()
	if cacheSize != 0 {
		t.Fatalf("default Responses path populated semantic cache with %d entries", cacheSize)
	}
}

func TestResponsesSemanticCachePGVectorRepositoryIsOptInAndFailClosed(t *testing.T) {
	t.Setenv(responseSemanticCacheEnv, "candidate")
	t.Setenv(responseSemanticCacheWorkspacesEnv, "ws_alpha")
	t.Setenv(responseSemanticCachePGVectorEnv, "true")
	t.Setenv(responseSemanticCachePGVectorWorkspacesEnv, "ws_alpha")
	server, handler, _ := newResponseTestServer(t)
	repository := &responseSemanticCacheRepositoryStub{}
	server.SetResponseSemanticCacheRepository(repository)

	create := func(input string) *httptest.ResponseRecorder {
		t.Helper()
		request := httptest.NewRequest(http.MethodPost, "/v1/responses", strings.NewReader(`{"model":"workama-chat","input":"`+input+`","temperature":0,"region":"global","guard_policy_version":"guard-v1","data_classification":"C2","semantic_cache":true}`))
		request.Header.Set("Authorization", "Bearer alpha-key")
		result := httptest.NewRecorder()
		handler.ServeHTTP(result, request)
		if result.Code != http.StatusOK {
			t.Fatalf("status = %d, body = %s", result.Code, result.Body.String())
		}
		return result
	}

	create("Persistent semantic candidate")
	store := server.responseStore()
	store.mu.Lock()
	store.semanticCache = make(map[string]responseSemanticCacheEntry)
	store.mu.Unlock()
	second := create("Persistent semantic candidate!")
	if second.Header().Get("x-wama-cache-provenance") != "semantic" {
		t.Fatalf("persistent candidate provenance = %q", second.Header().Get("x-wama-cache-provenance"))
	}
	repository.mu.Lock()
	lookups, puts := repository.lookups, repository.puts
	repository.mu.Unlock()
	if lookups == 0 || puts == 0 {
		t.Fatalf("repository calls = lookups:%d puts:%d", lookups, puts)
	}

	repository.mu.Lock()
	repository.lookupErr = errors.New("pgvector unavailable")
	repository.mu.Unlock()
	// The repository failure must not turn an otherwise valid in-memory hit into an API failure.
	third := create("Persistent semantic candidate!!")
	if third.Code != http.StatusOK {
		t.Fatalf("fail-closed fallback status = %d, body = %s", third.Code, third.Body.String())
	}
}

func TestResponsesSemanticCachePGVectorRequiresExplicitWorkspaceAllowlist(t *testing.T) {
	t.Setenv(responseSemanticCacheEnv, "candidate")
	t.Setenv(responseSemanticCacheWorkspacesEnv, "ws_alpha")
	t.Setenv(responseSemanticCachePGVectorEnv, "true")
	t.Setenv(responseSemanticCachePGVectorWorkspacesEnv, "ws_other")
	if responseSemanticCachePGVectorAllowedForWorkspace("ws_alpha") {
		t.Fatal("pgvector persistence bypassed its workspace allowlist")
	}
	t.Setenv(responseSemanticCachePGVectorEnv, "")
	t.Setenv(responseSemanticCachePGVectorWorkspacesEnv, "ws_alpha")
	if responseSemanticCachePGVectorAllowedForWorkspace("ws_alpha") {
		t.Fatal("pgvector persistence was enabled by default")
	}
}

type responseSemanticCacheSQLExecutorTestStub struct {
	mu        sync.Mutex
	row       []any
	queries   []string
	queryArgs [][]any
	execQuery string
	execArgs  []any
}

func (stub *responseSemanticCacheSQLExecutorTestStub) QueryContext(_ context.Context, query string, args ...any) (ResponseSemanticCacheSQLRows, error) {
	stub.mu.Lock()
	defer stub.mu.Unlock()
	stub.queries = append(stub.queries, query)
	stub.queryArgs = append(stub.queryArgs, append([]any(nil), args...))
	return &responseSemanticCacheSQLRowsTestStub{row: append([]any(nil), stub.row...)}, nil
}

func (stub *responseSemanticCacheSQLExecutorTestStub) ExecContext(_ context.Context, query string, args ...any) error {
	stub.mu.Lock()
	defer stub.mu.Unlock()
	stub.execQuery = query
	stub.execArgs = append([]any(nil), args...)
	return nil
}

type responseSemanticCacheSQLRowsTestStub struct {
	row     []any
	current bool
	closed  bool
}

func (rows *responseSemanticCacheSQLRowsTestStub) Next() bool {
	if rows.closed || rows.current {
		return false
	}
	rows.current = true
	return true
}

func (rows *responseSemanticCacheSQLRowsTestStub) Scan(dest ...any) error {
	if !rows.current || len(dest) != len(rows.row) {
		return errors.New("invalid test row scan")
	}
	for index, value := range rows.row {
		target := reflect.ValueOf(dest[index])
		if target.Kind() != reflect.Ptr || target.IsNil() {
			return errors.New("test row destination is not a pointer")
		}
		source := reflect.ValueOf(value)
		if !source.IsValid() || !source.Type().AssignableTo(target.Elem().Type()) {
			return errors.New("test row value type mismatch")
		}
		target.Elem().Set(source)
	}
	return nil
}

func (rows *responseSemanticCacheSQLRowsTestStub) Close() error {
	rows.closed = true
	return nil
}

func (rows *responseSemanticCacheSQLRowsTestStub) Err() error { return nil }

func TestResponsesSemanticCachePGVectorSQLRepositoryUsesMigrationScopeAndVector(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Microsecond)
	embedding := responseSemanticEmbedding("persistent semantic cache")
	scope := responseSemanticCacheScope{
		WorkspaceID: "ws_alpha", Model: "workama-chat", Provider: "mock", ChannelID: "chn_mock",
		UpstreamModel: "workama-chat", Capability: "responses.text", PromptID: "prompt-1", PromptVersion: 2,
		PromptChecksum: "checksum-1", GuardPolicyVersion: "guard-v1", DataClassification: "C2",
		OutputSignature: "signature-1", Region: "global",
	}
	row := []any{
		"cached output", 7, now, now.Add(time.Minute),
		scope.WorkspaceID, scope.Model, scope.Provider, scope.ChannelID, scope.UpstreamModel,
		scope.Capability, scope.PromptID, scope.PromptVersion, scope.PromptChecksum,
		scope.GuardPolicyVersion, scope.DataClassification, scope.OutputSignature, scope.Region,
		func() string { value, _ := responseSemanticCacheVectorText(embedding); return value }(),
	}
	executor := &responseSemanticCacheSQLExecutorTestStub{row: row}
	repository := &pgvectorResponseSemanticCache{executor: executor}
	result, err := repository.Lookup(context.Background(), ResponseSemanticCacheLookupRequest{
		Key: "resp_cache_key", Scope: scope, Embedding: embedding, Threshold: 0.97,
		Now: now, MaxCandidates: 0,
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.Exact == nil || result.Exact.Text != "cached output" || result.Exact.Scope != scope || len(result.Exact.Embedding) != responseSemanticCacheEmbeddingDimensions {
		t.Fatalf("exact result = %#v", result.Exact)
	}

	if err := repository.Put(context.Background(), "resp_cache_key", ResponseSemanticCacheEntry{
		Text: "cached output", CompletionTokens: 7, CreatedAt: now, ExpiresAt: now.Add(time.Minute),
		Scope: scope, Embedding: embedding,
	}); err != nil {
		t.Fatal(err)
	}
	executor.mu.Lock()
	queries := append([]string(nil), executor.queries...)
	execQuery := executor.execQuery
	execArgs := append([]any(nil), executor.execArgs...)
	executor.mu.Unlock()
	if len(queries) != 1 || !strings.Contains(queries[0], "gw_response_semantic_cache") || !strings.Contains(queries[0], "embedding::text") {
		t.Fatalf("lookup query = %#v", queries)
	}
	if !strings.Contains(execQuery, "ON CONFLICT (cache_key) DO UPDATE") || !strings.Contains(execQuery, "$17::vector") {
		t.Fatalf("put query = %q", execQuery)
	}
	if len(execArgs) != 19 || execArgs[16] != func() string { value, _ := responseSemanticCacheVectorText(embedding); return value }() {
		t.Fatalf("put args = %#v", execArgs)
	}
}

func TestResponsesSemanticCachePGVectorConfigurationAndLifecycle(t *testing.T) {
	t.Setenv(responseSemanticCacheEnv, "")
	t.Setenv(responseSemanticCacheWorkspacesEnv, "ws_alpha")
	t.Setenv(responseSemanticCachePGVectorEnv, "true")
	t.Setenv(responseSemanticCachePGVectorWorkspacesEnv, "ws_alpha")
	t.Setenv(responseSemanticCacheDatabaseURLEnv, "postgres://workama:secret@127.0.0.1:5432/workama")
	if responseSemanticCachePGVectorConfigured() {
		t.Fatal("pgvector configuration bypassed the default semantic-cache switch")
	}

	t.Setenv(responseSemanticCacheEnv, "candidate")
	if !responseSemanticCachePGVectorConfigured() {
		t.Fatal("valid pgvector configuration was rejected")
	}
	executor := &responseSemanticCacheSQLExecutorTestStub{}
	SetResponseSemanticCacheSQLExecutorFactory(func(_ string) (ResponseSemanticCacheSQLExecutor, io.Closer, error) {
		return executor, nil, nil
	})
	defer SetResponseSemanticCacheSQLExecutorFactory(nil)
	server, _, _ := newResponseTestServer(t)
	store := server.responseStore()
	store.mu.RLock()
	repository := store.semanticCacheRepository
	store.mu.RUnlock()
	if _, ok := repository.(*pgvectorResponseSemanticCache); !ok {
		t.Fatalf("configured repository = %T", repository)
	}
	if err := server.Close(); err != nil {
		t.Fatal(err)
	}
}

type responseSemanticCacheClosableRepository struct {
	responseSemanticCacheRepositoryStub
	mu     sync.Mutex
	closed bool
}

func (repository *responseSemanticCacheClosableRepository) Close() error {
	repository.mu.Lock()
	repository.closed = true
	repository.mu.Unlock()
	return nil
}

func TestResponsesSemanticCacheServerCloseClosesInjectedRepository(t *testing.T) {
	server, _, _ := newResponseTestServer(t)
	repository := &responseSemanticCacheClosableRepository{}
	server.SetResponseSemanticCacheRepository(repository)
	if err := server.Close(); err != nil {
		t.Fatal(err)
	}
	repository.mu.Lock()
	closed := repository.closed
	repository.mu.Unlock()
	if !closed {
		t.Fatal("injected semantic cache repository was not closed")
	}
}

func TestResponsesSemanticCachePGVectorConfigurationRequiresDatabaseAndExplicitAllowlist(t *testing.T) {
	t.Setenv(responseSemanticCacheEnv, "candidate")
	t.Setenv(responseSemanticCacheWorkspacesEnv, "ws_alpha")
	t.Setenv(responseSemanticCachePGVectorEnv, "true")
	t.Setenv(responseSemanticCachePGVectorWorkspacesEnv, "ws_alpha")
	t.Setenv(responseSemanticCacheDatabaseURLEnv, "")
	if responseSemanticCachePGVectorConfigured() {
		t.Fatal("pgvector configured without DATABASE_URL")
	}

	t.Setenv(responseSemanticCacheDatabaseURLEnv, "postgresql://workama:secret@db/workama")
	if !responseSemanticCachePGVectorConfigured() {
		t.Fatal("pgvector was not configured with all production gates enabled")
	}
	t.Setenv(responseSemanticCachePGVectorWorkspacesEnv, "*")
	if responseSemanticCachePGVectorConfigured() {
		t.Fatal("pgvector accepted wildcard-only workspace allowlist")
	}
	t.Setenv(responseSemanticCachePGVectorEnv, "")
	t.Setenv(responseSemanticCachePGVectorWorkspacesEnv, "ws_alpha")
	if responseSemanticCachePGVectorConfigured() {
		t.Fatal("pgvector enabled without explicit feature flag")
	}
}

func TestPGVectorResponseSemanticCacheSQLRoundTripMapping(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Microsecond)
	scope := responseSemanticCacheScope{
		WorkspaceID: "ws_alpha", Model: "workama-chat", Provider: "mock", ChannelID: "chn_mock",
		UpstreamModel: "workama-chat", Capability: "responses.text", PromptID: "prompt-1",
		PromptVersion: 3, PromptChecksum: "checksum", GuardPolicyVersion: "guard-v1",
		DataClassification: "C2", OutputSignature: "signature", Region: "global",
	}
	entry := responseSemanticCacheEntry{
		Text: "cached completion", CompletionTokens: 7, CreatedAt: now, ExpiresAt: now.Add(time.Minute),
		Scope: scope, Embedding: responseSemanticEmbedding("cached prompt"),
	}
	vector, err := responseSemanticCacheVectorText(entry.Embedding)
	if err != nil {
		t.Fatal(err)
	}

	executor := &responseSemanticCacheSQLExecutorStub{
		exactRows: [][]any{responseSemanticCacheSQLEntryRow(entry, vector)},
	}
	repository := &pgvectorResponseSemanticCache{executor: executor}
	result, err := repository.Lookup(context.Background(), ResponseSemanticCacheLookupRequest{
		Key: "cache-key", Scope: scope, Embedding: entry.Embedding, Threshold: 0.97,
		MaxCandidates: 4, Now: now,
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.Exact == nil || result.Exact.Text != entry.Text || result.Exact.Scope != scope || len(result.Exact.Embedding) != responseSemanticCacheEmbeddingDimensions {
		t.Fatalf("exact result = %#v", result.Exact)
	}
	if len(executor.queries) != 1 || len(executor.args[0]) != 15 || executor.args[0][0] != "cache-key" {
		t.Fatalf("exact query args = %#v", executor.args)
	}
	if !strings.Contains(executor.queries[0], "gw_response_semantic_cache") || !strings.Contains(executor.queries[0], "embedding::text") {
		t.Fatalf("exact query did not target 032 schema: %s", executor.queries[0])
	}

	executor.exactRows = nil
	executor.candidateRows = [][]any{responseSemanticCacheSQLCandidateRow("other-key", entry, vector, 0.985)}
	result, err = repository.Lookup(context.Background(), ResponseSemanticCacheLookupRequest{
		Key: "cache-key", Scope: scope, Embedding: entry.Embedding, Threshold: 0.97,
		MaxCandidates: 4, Now: now,
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Candidates) != 1 || result.Candidates[0].Key != "other-key" || result.Candidates[0].Similarity != 0.985 {
		t.Fatalf("candidate result = %#v", result.Candidates)
	}
	if len(executor.queries) != 3 || len(executor.args[2]) != 17 || executor.args[2][0] != vector {
		t.Fatalf("candidate query args = %#v", executor.args)
	}
	if !strings.Contains(executor.queries[2], "embedding <=> $1::vector") || !strings.Contains(executor.queries[2], "ORDER BY") {
		t.Fatalf("candidate query did not use cosine pgvector search: %s", executor.queries[2])
	}

	if err := repository.Put(context.Background(), "cache-key", entry); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(executor.execQuery, "ON CONFLICT (cache_key)") || len(executor.execArgs) != 19 || executor.execArgs[16] != vector {
		t.Fatalf("put query/args = %s %#v", executor.execQuery, executor.execArgs)
	}
}

func responseSemanticCacheSQLEntryRow(entry responseSemanticCacheEntry, vector string) []any {
	return []any{
		entry.Text, entry.CompletionTokens, entry.CreatedAt, entry.ExpiresAt,
		entry.Scope.WorkspaceID, entry.Scope.Model, entry.Scope.Provider, entry.Scope.ChannelID,
		entry.Scope.UpstreamModel, entry.Scope.Capability, entry.Scope.PromptID, entry.Scope.PromptVersion,
		entry.Scope.PromptChecksum, entry.Scope.GuardPolicyVersion, entry.Scope.DataClassification,
		entry.Scope.OutputSignature, entry.Scope.Region, vector,
	}
}

func responseSemanticCacheSQLCandidateRow(key string, entry responseSemanticCacheEntry, vector string, similarity float64) []any {
	row := responseSemanticCacheSQLEntryRow(entry, vector)
	return append([]any{key}, append(row, similarity)...)
}

func TestResponsesSemanticCacheRepositoryTimeoutFailsClosedToRequestPath(t *testing.T) {
	t.Setenv(responseSemanticCacheEnv, "candidate")
	t.Setenv(responseSemanticCacheWorkspacesEnv, "ws_alpha")
	t.Setenv(responseSemanticCachePGVectorEnv, "true")
	t.Setenv(responseSemanticCachePGVectorWorkspacesEnv, "ws_alpha")
	server, _, _ := newResponseTestServer(t)
	server.SetResponseSemanticCacheRepository(blockingResponseSemanticCacheRepository{})
	record := &responseRecord{
		semanticCacheKey:       "slow-key",
		semanticCacheScope:     responseSemanticCacheScope{WorkspaceID: "ws_alpha"},
		semanticCacheEmbedding: responseSemanticEmbedding("slow lookup"),
		semanticCacheSafe:      true,
	}
	started := time.Now()
	_, _, _, ok := server.responseSemanticCacheLookup(record)
	if ok {
		t.Fatal("timed out repository unexpectedly returned a cache hit")
	}
	if elapsed := time.Since(started); elapsed < responseSemanticCacheRepositoryTimeout || elapsed > 500*time.Millisecond {
		t.Fatalf("repository timeout elapsed = %v", elapsed)
	}
}

func TestResponsesServerCloseReleasesInjectedSemanticCacheRepository(t *testing.T) {
	server := &Server{Logger: slog.Default()}
	repository := &closableResponseSemanticCacheRepository{}
	server.SetResponseSemanticCacheRepository(repository)
	if err := server.Close(); err != nil {
		t.Fatal(err)
	}
	if repository.closed != 1 {
		t.Fatalf("repository close count = %d, want 1", repository.closed)
	}
	if err := server.Close(); err != nil {
		t.Fatal(err)
	}
	if repository.closed != 1 {
		t.Fatalf("repository close count after second Close = %d, want 1", repository.closed)
	}
}
