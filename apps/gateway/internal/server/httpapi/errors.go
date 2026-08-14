// Package httpapi implements the gateway's HTTP handlers for the
// 10-step middleware pipeline. It exposes /v1/chat/completions and
// /v1/models endpoints backed by the relay/routing/adapter packages.
package httpapi

import (
	"encoding/json"
	"net/http"

	"github.com/workama/workama/apps/gateway/internal/server/httperr"
)

// ErrorCode is a re-export of httperr.ErrorCode for handler-internal use.
type ErrorCode = httperr.ErrorCode

// Re-export the canonical error codes so handlers can reference them
// without importing the httperr package directly.
const (
	CodeBadRequest          = httperr.CodeBadRequest
	CodeUnauthorized        = httperr.CodeUnauthorized
	CodeForbidden           = httperr.CodeForbidden
	CodeInsufficientBalance = httperr.CodeInsufficientBalance
	CodeRateLimited         = httperr.CodeRateLimited
	CodeNoChannel           = httperr.CodeNoChannel
	CodeGatewayError        = httperr.CodeGatewayError
	CodeUpstreamError       = httperr.CodeUpstreamError
	CodeUpstreamStatus      = httperr.CodeUpstreamStatus
)

// WriteError writes an OpenAI-compatible error JSON response.
//
//	{"error": {"code": "E01006", "message": "...", "type": "invalid_request_error"}}
func WriteError(w http.ResponseWriter, code ErrorCode, message string) {
	httperr.Write(w, code, message)
}

// WriteErrorWithStatus allows callers to override the HTTP status code.
func WriteErrorWithStatus(w http.ResponseWriter, code ErrorCode, message string, status int) {
	httperr.WriteWithStatus(w, code, message, status)
}

// writeJSON is the internal helper used by handlers.
func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
