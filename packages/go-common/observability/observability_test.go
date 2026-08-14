package observability

import (
	"strings"
	"testing"
)

func TestSemanticMappingsAreStableAndContentFree(t *testing.T) {
	genAI := GenAIAttributes("chat", "workama-chat", "mock", "succeeded", "", 4, 7, 0.03)
	if len(genAI) == 0 || genAI[0].Key != "ai.operation" || genAI[0].Value.AsString() != "chat" {
		t.Fatalf("unexpected genai attributes: %#v", genAI)
	}
	mcp := MCPAttributes("server-secret-id", "stdio", "tools/call", "tools", "succeeded", "low", "")
	if len(mcp) == 0 || mcp[0].Key != "mcp.server_id_hash" || len(mcp[0].Value.AsString()) != 16 {
		t.Fatalf("unexpected mcp attributes: %#v", mcp)
	}
	for _, attr := range mcp {
		if strings.Contains(attr.Value.AsString(), "server-secret-id") { t.Fatal("raw server identity leaked") }
	}
}
