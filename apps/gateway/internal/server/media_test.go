package server

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestMediaMockContractsReturnControlledRefsAndNoRawInput(t *testing.T) {
	_, handler, _ := newResponseTestServer(t)
	tests := []struct {
		name string
		path string
		body string
		want string
	}{
		{name: "image", path: "/v1/images/generations", body: `{"model":"workama-image","prompt":"a product card"}`, want: "mock://image/"},
		{name: "edit", path: "/v1/images/edits", body: `{"model":"workama-image","prompt":"add a badge","image_ref":"mock://image/source"}`, want: "mock://image/"},
		{name: "speech", path: "/v1/audio/speech", body: `{"model":"workama-tts","input":"hello audio","voice":"alloy"}`, want: "mock://audio/"},
		{name: "transcription", path: "/v1/audio/transcriptions", body: `{"model":"workama-stt","input_ref":"mock://audio/source"}`, want: "WorkAMA local transcription"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			request := httptest.NewRequest(http.MethodPost, test.path, strings.NewReader(test.body))
			request.Header.Set("Authorization", "Bearer alpha-key")
			result := httptest.NewRecorder()
			handler.ServeHTTP(result, request)
			if result.Code != http.StatusOK || !strings.Contains(result.Body.String(), test.want) {
				t.Fatalf("media result = %d %s", result.Code, result.Body.String())
			}
			if strings.Contains(result.Body.String(), "hello audio") && test.name != "speech" {
				t.Fatal("unexpected raw input in media result")
			}
		})
	}
}

func TestMediaRejectsMissingInputAndInvalidImageCount(t *testing.T) {
	_, handler, _ := newResponseTestServer(t)
	for path, body := range map[string]string{
		"/v1/images/generations":   `{"model":"workama-image","prompt":"x","n":5}`,
		"/v1/images/edits":         `{"model":"workama-image","prompt":"x"}`,
		"/v1/audio/speech":         `{"model":"workama-tts","input":"x"}`,
		"/v1/audio/transcriptions": `{"model":"workama-stt"}`,
	} {
		request := httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
		request.Header.Set("Authorization", "Bearer alpha-key")
		result := httptest.NewRecorder()
		handler.ServeHTTP(result, request)
		if result.Code != http.StatusBadRequest {
			t.Fatalf("%s returned %d: %s", path, result.Code, result.Body.String())
		}
	}
}
