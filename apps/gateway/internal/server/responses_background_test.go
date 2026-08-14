package server

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/workama/workama/apps/gateway/internal/relay"
)

func TestBackgroundResponsePersistenceIncludesChatBodyAndPromptTokens(t *testing.T) {
	persistence := newMemoryResponsePersistence()
	registry := newResponseRegistry(persistence, time.Hour, slog.Default())
	registry.ensure(slog.Default())

	record := &responseRecord{
		workspaceID:  "ws_1",
		model:        "workama-chat",
		requestID:    "req_1",
		background:   true,
		expiresAt:    registry.now().Add(time.Hour),
		promptTokens: 42,
		chatBody:     ChatRequest{Model: "workama-chat", Messages: []Message{{Role: "user", Content: "hello"}}},
		object: responseObject{
			ID: "resp_1", Object: "response", Status: "queued", Model: "workama-chat",
			Output: []responseOutputItem{}, Metadata: map[string]string{},
		},
		cancel: func() {},
	}
	registry.records["resp_1"] = record
	registry.persist(record)

	loaded, err := persistence.Load()
	if err != nil {
		t.Fatal(err)
	}
	persisted, ok := loaded["resp_1"]
	if !ok {
		t.Fatal("persisted record not found")
	}
	if persisted.PromptTokens != 42 {
		t.Fatalf("PromptTokens = %d, want 42", persisted.PromptTokens)
	}
	if persisted.ChatBody.Model != "workama-chat" {
		t.Fatalf("ChatBody.Model = %q, want workama-chat", persisted.ChatBody.Model)
	}
	if len(persisted.ChatBody.Messages) != 1 || persisted.ChatBody.Messages[0].Content != "hello" {
		t.Fatalf("ChatBody.Messages = %#v", persisted.ChatBody.Messages)
	}
}

func TestRegistryEnsureResetsInProgressBackgroundToQueued(t *testing.T) {
	now := time.Now().UTC()
	persistence := newMemoryResponsePersistence()
	persistence.Save("resp_inprog", persistedResponseRecord{
		WorkspaceID:  "ws_1",
		Model:        "workama-chat",
		RequestID:    "req_1",
		Background:   true,
		ExpiresAt:    now.Add(time.Hour),
		PromptTokens: 10,
		ChatBody:     ChatRequest{Model: "workama-chat", Messages: []Message{{Role: "user", Content: "test"}}},
		Object: responseObject{
			ID: "resp_inprog", Object: "response", Status: "in_progress", Model: "workama-chat",
			Output: []responseOutputItem{}, Metadata: map[string]string{},
		},
	})

	registry := newResponseRegistry(persistence, time.Hour, slog.Default())
	registry.now = func() time.Time { return now }
	registry.ensure(slog.Default())

	record, ok := registry.get("resp_inprog")
	if !ok {
		t.Fatal("in_progress record was not restored")
	}
	if record.object.Status != "queued" {
		t.Fatalf("restored status = %q, want queued", record.object.Status)
	}
	if record.object.IncompleteDetails == nil || record.object.IncompleteDetails.Reason != "recovered_after_restart" {
		t.Fatalf("incomplete_details = %#v", record.object.IncompleteDetails)
	}
	if record.promptTokens != 10 {
		t.Fatalf("promptTokens = %d, want 10", record.promptTokens)
	}
	if record.chatBody.Messages[0].Content != "test" {
		t.Fatalf("chatBody not restored")
	}
}

func TestRegistryEnsureDropsExpiredBackgroundRecords(t *testing.T) {
	now := time.Now().UTC()
	persistence := newMemoryResponsePersistence()
	persistence.Save("resp_expired", persistedResponseRecord{
		WorkspaceID: "ws_1",
		Model:       "workama-chat",
		Background:  true,
		ExpiresAt:   now.Add(-time.Minute),
		Object: responseObject{
			ID: "resp_expired", Status: "queued", Model: "workama-chat",
		},
	})

	registry := newResponseRegistry(persistence, time.Hour, slog.Default())
	registry.now = func() time.Time { return now }
	registry.ensure(slog.Default())

	if _, ok := registry.get("resp_expired"); ok {
		t.Fatal("expired background record was restored")
	}
}

func TestRecoverBackgroundResponsesRestartsQueuedTasks(t *testing.T) {
	var resolveCalls int
	var resolveMu sync.Mutex
	platform := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/internal/gateway/resolve":
			resolveMu.Lock()
			resolveCalls++
			resolveMu.Unlock()
			writeJSON(w, http.StatusOK, map[string]any{
				"workspace_id": "ws_alpha", "rpm_limit": 60, "tpm_limit": 100000,
				"channels": []map[string]any{{"id": "chn_mock", "provider": "mock", "base_url": "mock://local", "api_key": "secret"}},
			})
		case "/internal/gateway/rate-limit/batch":
			writeJSON(w, http.StatusOK, map[string]any{"allowed": true})
		case "/internal/gateway/reserve":
			writeJSON(w, http.StatusOK, map[string]any{"reservation_id": "res_1", "estimated_cost": 1, "status": "active"})
		case "/internal/security/moderate":
			writeJSON(w, http.StatusOK, map[string]any{"action": "allow", "text": ""})
		case "/internal/gateway/release":
			w.WriteHeader(http.StatusNoContent)
		default:
			http.NotFound(w, r)
		}
	}))
	defer platform.Close()

	server := New(relay.NewPlatformClient(platform.URL, "internal"), nil, "internal", slog.New(slog.NewTextHandler(io.Discard, nil)))
	now := time.Now().UTC()
	store := server.responseStore()
	store.now = func() time.Time { return now }

	record := &responseRecord{
		workspaceID:  "ws_alpha",
		model:        "workama-chat",
		requestID:    "req_1",
		background:   true,
		expiresAt:    now.Add(time.Hour),
		promptTokens: 5,
		chatBody:     ChatRequest{Model: "workama-chat", Messages: []Message{{Role: "user", Content: "recover me"}}},
		object: responseObject{
			ID: "resp_recover", Object: "response", Status: "queued", Model: "workama-chat",
			Output: []responseOutputItem{}, Metadata: map[string]string{},
		},
		cancel: func() {},
	}
	store.mu.Lock()
	store.records["resp_recover"] = record
	store.mu.Unlock()

	server.recoverBackgroundResponses()

	// Wait for the background goroutine to complete
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		store.mu.RLock()
		status := record.object.Status
		store.mu.RUnlock()
		if status == "completed" || status == "failed" {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}

	resolveMu.Lock()
	calls := resolveCalls
	resolveMu.Unlock()
	if calls != 1 {
		t.Fatalf("resolve calls = %d, want 1", calls)
	}

	if record.object.Status != "completed" && record.object.Status != "failed" {
		t.Fatalf("unexpected final status = %q", record.object.Status)
	}
}

func TestRecoverBackgroundResponsesFailsOnResolveError(t *testing.T) {
	platform := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
	}))
	defer platform.Close()

	server := New(relay.NewPlatformClient(platform.URL, "internal"), nil, "internal", slog.New(slog.NewTextHandler(io.Discard, nil)))
	now := time.Now().UTC()
	store := server.responseStore()
	store.now = func() time.Time { return now }

	record := &responseRecord{
		workspaceID:  "ws_alpha",
		model:        "workama-chat",
		requestID:    "req_1",
		background:   true,
		expiresAt:    now.Add(time.Hour),
		promptTokens: 5,
		chatBody:     ChatRequest{Model: "workama-chat", Messages: []Message{{Role: "user", Content: "fail me"}}},
		object: responseObject{
			ID: "resp_fail", Object: "response", Status: "queued", Model: "workama-chat",
			Output: []responseOutputItem{}, Metadata: map[string]string{},
		},
		cancel: func() {},
	}
	store.mu.Lock()
	store.records["resp_fail"] = record
	store.mu.Unlock()

	server.recoverBackgroundResponses()

	if record.object.Status != "failed" {
		t.Fatalf("status = %q, want failed", record.object.Status)
	}
	if record.object.Error == nil || !strings.Contains(record.object.Error.Message, "restart") {
		t.Fatalf("error = %#v", record.object.Error)
	}
}

func TestMarkResponseInProgressOnlyFromQueued(t *testing.T) {
	server := New(nil, nil, "internal", slog.Default())
	now := time.Now().UTC()
	store := server.responseStore()
	store.now = func() time.Time { return now }

	record := &responseRecord{
		object: responseObject{ID: "resp_1", Status: "queued"},
	}
	store.records["resp_1"] = record

	if !server.markResponseInProgress(record) {
		t.Fatal("expected true for queued")
	}
	if record.object.Status != "in_progress" {
		t.Fatalf("status = %q", record.object.Status)
	}
	if server.markResponseInProgress(record) {
		t.Fatal("expected false for in_progress")
	}
}

func TestCompleteResponseRejectsCancelledOrFailed(t *testing.T) {
	server := New(nil, nil, "internal", slog.Default())
	now := time.Now().UTC()
	store := server.responseStore()
	store.now = func() time.Time { return now }

	cancelled := &responseRecord{object: responseObject{ID: "resp_c", Status: "cancelled"}}
	if server.completeResponse(cancelled, "text", 1, 1) {
		t.Fatal("complete should fail for cancelled")
	}

	failed := &responseRecord{object: responseObject{ID: "resp_f", Status: "failed"}}
	if server.completeResponse(failed, "text", 1, 1) {
		t.Fatal("complete should fail for failed")
	}

	inProgress := &responseRecord{object: responseObject{ID: "resp_i", Status: "in_progress"}}
	if !server.completeResponse(inProgress, "done", 1, 2) {
		t.Fatal("complete should succeed for in_progress")
	}
	if inProgress.object.Status != "completed" || inProgress.object.OutputText != "done" {
		t.Fatalf("completed record = %#v", inProgress.object)
	}
	if inProgress.object.Usage == nil || inProgress.object.Usage.TotalTokens != 3 {
		t.Fatalf("usage = %#v", inProgress.object.Usage)
	}
}

func TestFailResponseRejectsCancelledOrCompleted(t *testing.T) {
	server := New(nil, nil, "internal", slog.Default())
	now := time.Now().UTC()
	store := server.responseStore()
	store.now = func() time.Time { return now }

	cancelled := &responseRecord{object: responseObject{ID: "resp_c", Status: "cancelled"}}
	if server.failResponse(cancelled, "code", "msg") {
		t.Fatal("fail should return false for cancelled")
	}

	completed := &responseRecord{object: responseObject{ID: "resp_d", Status: "completed"}}
	if server.failResponse(completed, "code", "msg") {
		t.Fatal("fail should return false for completed")
	}

	inProgress := &responseRecord{object: responseObject{ID: "resp_i", Status: "in_progress"}}
	if !server.failResponse(inProgress, "err_code", "err_msg") {
		t.Fatal("fail should return true for in_progress")
	}
	if inProgress.object.Status != "failed" {
		t.Fatalf("status = %q", inProgress.object.Status)
	}
	if inProgress.object.Error == nil || inProgress.object.Error.Code != "err_code" {
		t.Fatalf("error = %#v", inProgress.object.Error)
	}
}

func TestCancelResponseRecordIdempotentAndScoped(t *testing.T) {
	server := New(nil, nil, "internal", slog.Default())
	now := time.Now().UTC()
	store := server.responseStore()
	store.now = func() time.Time { return now }

	record := &responseRecord{
		object: responseObject{ID: "resp_1", Status: "queued"},
		cancel: func() {},
	}
	store.records["resp_1"] = record

	if !server.cancelResponseRecord(record) {
		t.Fatal("first cancel should succeed")
	}
	if record.object.Status != "cancelled" {
		t.Fatalf("status = %q", record.object.Status)
	}
	if !server.cancelResponseRecord(record) {
		t.Fatal("second cancel should be idempotent true")
	}

	completed := &responseRecord{object: responseObject{ID: "resp_2", Status: "completed"}}
	if server.cancelResponseRecord(completed) {
		t.Fatal("cancel should fail for completed")
	}
}

func TestBackgroundResponseStateMachineTransitions(t *testing.T) {
	// End-to-end state machine test through the HTTP surface.
	_, handler, control := newResponseTestServer(t)
	request := httptest.NewRequest(http.MethodPost, "/v1/responses", strings.NewReader(`{"model":"workama-chat","input":"state machine","background":true}`))
	request.Header.Set("Authorization", "Bearer alpha-key")
	created := httptest.NewRecorder()
	handler.ServeHTTP(created, request)
	if created.Code != http.StatusAccepted {
		t.Fatalf("create status = %d", created.Code)
	}
	var response responseObject
	if err := json.Unmarshal(created.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Status != "queued" {
		t.Fatalf("initial status = %q", response.Status)
	}

	// Poll until completion
	deadline := time.Now().Add(time.Second)
	var finalStatus string
	for time.Now().Before(deadline) {
		poll := httptest.NewRequest(http.MethodGet, "/v1/responses/"+response.ID, nil)
		poll.Header.Set("Authorization", "Bearer alpha-key")
		polled := httptest.NewRecorder()
		handler.ServeHTTP(polled, poll)
		var current responseObject
		_ = json.Unmarshal(polled.Body.Bytes(), &current)
		finalStatus = current.Status
		if current.Status == "completed" {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if finalStatus != "completed" {
		t.Fatalf("final status = %q", finalStatus)
	}

	control.mu.Lock()
	releases := control.releases
	control.mu.Unlock()
	if releases != 0 {
		t.Fatalf("unexpected releases = %d", releases)
	}
}
