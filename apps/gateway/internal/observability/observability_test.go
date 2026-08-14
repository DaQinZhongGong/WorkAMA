package observability

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestRequestIDValidation(t *testing.T) {
	for _, value := range []string{"req_01KXTEST", "client-request.123"} {
		if !ValidRequestID(value) { t.Fatalf("valid request id rejected: %s", value) }
	}
	for _, value := range []string{"", "bad\nheader", strings.Repeat("x", 129)} {
		if ValidRequestID(value) { t.Fatalf("invalid request id accepted: %q", value) }
	}
}

func TestMiddlewareReturnsRequestAndTraceHeaders(t *testing.T) {
	handler := Middleware("gateway", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if RequestID(r.Context()) != "req_client" { t.Fatalf("context request id missing") }
		w.WriteHeader(http.StatusNoContent)
	}))
	request := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	request.Header.Set("X-Wama-Request-ID", "req_client")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Header().Get("X-Wama-Request-ID") != "req_client" { t.Fatal("response request id missing") }
	if !strings.HasPrefix(response.Header().Get("traceparent"), "00-") { t.Fatal("response traceparent missing") }
}
