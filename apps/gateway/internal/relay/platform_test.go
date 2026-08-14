package relay

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestModerateSendsWorkspaceDirectionAndInternalToken(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/internal/security/moderate" || r.Header.Get("X-Internal-Token") != "internal-secret" {
			t.Fatalf("unexpected request path or token")
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if body["workspace_id"] != "wsp_1" || body["direction"] != "input" || body["request_id"] != "req_1" {
			t.Fatalf("unexpected moderation body: %#v", body)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"action": "mask", "text": "safe ***", "matches": []string{"api_key"},
		})
	}))
	defer server.Close()

	client := NewPlatformClient(server.URL, "internal-secret")
	result, err := client.Moderate(context.Background(), "wsp_1", "input", "safe api_key", "req_1")
	if err != nil {
		t.Fatal(err)
	}
	if result.Action != "mask" || result.Text != "safe ***" || len(result.Matches) != 1 {
		t.Fatalf("unexpected moderation result: %#v", result)
	}
}
