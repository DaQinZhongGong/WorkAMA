package server

import (
	"log/slog"
	"path/filepath"
	"testing"
	"time"
)

func TestFileResponsePersistenceRoundTripAndDelete(t *testing.T) {
	path := filepath.Join(t.TempDir(), "responses.json")
	store := newFileResponsePersistence(path)
	want := persistedResponseRecord{
		WorkspaceID: "ws_1",
		Model:       "workama-chat",
		RequestID:   "req_1",
		ExpiresAt:   time.Now().Add(time.Hour).UTC().Truncate(time.Second),
		Object: responseObject{
			ID: "resp_1", Object: "response", Status: "completed", Model: "workama-chat",
			Output: []responseOutputItem{}, Metadata: map[string]string{"trace": "safe"},
		},
	}
	if err := store.Save(want.Object.ID, want); err != nil {
		t.Fatal(err)
	}
	loaded, err := store.Load()
	if err != nil {
		t.Fatal(err)
	}
	if loaded["resp_1"].WorkspaceID != want.WorkspaceID || loaded["resp_1"].Object.Status != "completed" {
		t.Fatalf("unexpected loaded response: %#v", loaded["resp_1"])
	}
	if err := store.Delete("resp_1"); err != nil {
		t.Fatal(err)
	}
	loaded, err = store.Load()
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := loaded["resp_1"]; ok {
		t.Fatal("deleted response remained in persistence")
	}
}

func TestResponseRegistryRestoresNonExpiredAndDropsExpiredRecords(t *testing.T) {
	now := time.Now().UTC()
	persistence := newMemoryResponsePersistence()
	if err := persistence.Save("resp_live", persistedResponseRecord{
		WorkspaceID: "ws_1", Model: "workama-chat", ExpiresAt: now.Add(time.Hour),
		Object: responseObject{ID: "resp_live", Status: "completed", Model: "workama-chat", Metadata: map[string]string{}},
	}); err != nil {
		t.Fatal(err)
	}
	if err := persistence.Save("resp_old", persistedResponseRecord{
		WorkspaceID: "ws_1", Model: "workama-chat", ExpiresAt: now.Add(-time.Minute),
		Object: responseObject{ID: "resp_old", Status: "completed", Model: "workama-chat", Metadata: map[string]string{}},
	}); err != nil {
		t.Fatal(err)
	}
	registry := newResponseRegistry(persistence, time.Hour, slog.Default())
	registry.now = func() time.Time { return now }
	registry.ensure(slog.Default())
	if _, ok := registry.get("resp_live"); !ok {
		t.Fatal("non-expired response was not restored")
	}
	if _, ok := registry.get("resp_old"); ok {
		t.Fatal("expired response was restored")
	}
}

func TestResponseRegistryGetExpiresAndRemovesRecord(t *testing.T) {
	registry := newResponseRegistry(newMemoryResponsePersistence(), time.Hour, slog.Default())
	registry.now = func() time.Time { return time.Unix(100, 0) }
	registry.ensure(slog.Default())
	registry.records["resp_old"] = &responseRecord{
		object: responseObject{ID: "resp_old"}, expiresAt: time.Unix(99, 0),
	}
	if _, ok := registry.get("resp_old"); ok {
		t.Fatal("expired response was returned")
	}
	if _, ok := registry.records["resp_old"]; ok {
		t.Fatal("expired response remained in memory")
	}
}
