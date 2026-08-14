// Package httperr provides shared OpenAI-compatible error response helpers
// for the gateway HTTP API and middleware. Splitting it out avoids an
// import cycle between server/httpapi and server/middleware.
package httperr

import (
	"encoding/json"
	"net/http"
)

// ErrorCode is the canonical gateway error code (E01xxx).
type ErrorCode string

const (
	// E00001: invalid JSON / missing required field
	CodeBadRequest ErrorCode = "E00001"
	// E01001: missing or invalid Authorization Bearer
	CodeUnauthorized ErrorCode = "E01001"
	// E01002: model not allowed for this key
	CodeForbidden ErrorCode = "E01002"
	// E01004: insufficient credit balance
	CodeInsufficientBalance ErrorCode = "E01004"
	// E01005: rate limit exceeded
	CodeRateLimited ErrorCode = "E01005"
	// E01006: no channel available for this model
	CodeNoChannel ErrorCode = "E01006"
	// E01007: gateway control plane / upstream error
	CodeGatewayError ErrorCode = "E01007"
	// E01050: upstream connection error
	CodeUpstreamError ErrorCode = "E01050"
	// E01051: upstream returned HTTP error
	CodeUpstreamStatus ErrorCode = "E01051"
)

// ErrorType maps an error code to an OpenAI-compatible error type.
func ErrorType(code ErrorCode) string {
	switch code {
	case CodeBadRequest, CodeNoChannel:
		return "invalid_request_error"
	case CodeUnauthorized:
		return "authentication_error"
	case CodeForbidden, CodeInsufficientBalance:
		return "authorization_error"
	case CodeRateLimited:
		return "rate_limit_error"
	case CodeUpstreamError, CodeUpstreamStatus, CodeGatewayError:
		return "api_error"
	default:
		return "api_error"
	}
}

// HTTPStatus maps an error code to an HTTP status code.
func HTTPStatus(code ErrorCode) int {
	switch code {
	case CodeBadRequest:
		return http.StatusBadRequest
	case CodeUnauthorized:
		return http.StatusUnauthorized
	case CodeForbidden:
		return http.StatusForbidden
	case CodeInsufficientBalance:
		return http.StatusPaymentRequired
	case CodeRateLimited:
		return http.StatusTooManyRequests
	case CodeNoChannel:
		return http.StatusNotFound
	case CodeUpstreamError, CodeUpstreamStatus:
		return http.StatusBadGateway
	case CodeGatewayError:
		return http.StatusServiceUnavailable
	default:
		return http.StatusInternalServerError
	}
}

// ErrorEnvelope is the OpenAI-compatible error response wrapper.
type ErrorEnvelope struct {
	Error ErrorBody `json:"error"`
}

// ErrorBody is the inner error detail.
type ErrorBody struct {
	Code    ErrorCode `json:"code"`
	Message string    `json:"message"`
	Type    string    `json:"type"`
	Param   any       `json:"param"`
}

// Write writes an OpenAI-compatible error JSON response with the default
// HTTP status code for the given error code.
//
//	{"error": {"code": "E01006", "message": "...", "type": "invalid_request_error"}}
func Write(w http.ResponseWriter, code ErrorCode, message string) {
	WriteWithStatus(w, code, message, HTTPStatus(code))
}

// WriteWithStatus allows callers to override the HTTP status code.
func WriteWithStatus(w http.ResponseWriter, code ErrorCode, message string, status int) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(ErrorEnvelope{
		Error: ErrorBody{
			Code:    code,
			Message: message,
			Type:    ErrorType(code),
			Param:   nil,
		},
	})
}
