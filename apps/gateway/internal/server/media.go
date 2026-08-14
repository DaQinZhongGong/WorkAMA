package server

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/workama/workama/apps/gateway/internal/metering"
	"github.com/workama/workama/apps/gateway/internal/relay"
)

type imageGenerationRequest struct {
	Model          string `json:"model"`
	Prompt         string `json:"prompt"`
	N              int    `json:"n,omitempty"`
	Size           string `json:"size,omitempty"`
	ResponseFormat string `json:"response_format,omitempty"`
}

type imageEditRequest struct {
	Model          string `json:"model"`
	Prompt         string `json:"prompt"`
	ImageRef       string `json:"image_ref"`
	MaskRef        string `json:"mask_ref,omitempty"`
	N              int    `json:"n,omitempty"`
	Size           string `json:"size,omitempty"`
	ResponseFormat string `json:"response_format,omitempty"`
}

type audioSpeechRequest struct {
	Model          string `json:"model"`
	Input          string `json:"input"`
	Voice          string `json:"voice"`
	ResponseFormat string `json:"response_format,omitempty"`
}

type audioTranscriptionRequest struct {
	Model    string `json:"model"`
	InputRef string `json:"input_ref"`
	Language string `json:"language,omitempty"`
}

func (s *Server) imagesGenerations(w http.ResponseWriter, r *http.Request) {
	var body imageGenerationRequest
	if err := decodeJSON(r.Body, &body); err != nil || strings.TrimSpace(body.Model) == "" || strings.TrimSpace(body.Prompt) == "" {
		writeOpenAIError(w, http.StatusBadRequest, "E00001", "model and prompt are required")
		return
	}
	if body.N == 0 {
		body.N = 1
	}
	if body.N < 1 || body.N > 4 {
		writeOpenAIError(w, http.StatusBadRequest, "E00001", "n must be between 1 and 4")
		return
	}
	s.renderMockImages(w, r, body.Model, body.Prompt, "generate", "", body.N, body.Size, body.ResponseFormat)
}

func (s *Server) imagesEdits(w http.ResponseWriter, r *http.Request) {
	var body imageEditRequest
	if err := decodeJSON(r.Body, &body); err != nil || strings.TrimSpace(body.Model) == "" || strings.TrimSpace(body.Prompt) == "" || strings.TrimSpace(body.ImageRef) == "" {
		writeOpenAIError(w, http.StatusBadRequest, "E00001", "model, prompt and image_ref are required")
		return
	}
	if body.N == 0 {
		body.N = 1
	}
	if body.N < 1 || body.N > 4 {
		writeOpenAIError(w, http.StatusBadRequest, "E00001", "n must be between 1 and 4")
		return
	}
	s.renderMockImages(w, r, body.Model, body.Prompt, "edit", body.ImageRef+"\n"+body.MaskRef, body.N, body.Size, body.ResponseFormat)
}

func (s *Server) audioSpeech(w http.ResponseWriter, r *http.Request) {
	var body audioSpeechRequest
	if err := decodeJSON(r.Body, &body); err != nil || strings.TrimSpace(body.Model) == "" || strings.TrimSpace(body.Input) == "" || strings.TrimSpace(body.Voice) == "" {
		writeOpenAIError(w, http.StatusBadRequest, "E00001", "model, input and voice are required")
		return
	}
	route, err := s.authenticate(r, body.Model)
	if err != nil {
		s.writeResolveError(w, err)
		return
	}
	if !s.enforceRateLimit(w, r, route, estimateTokens(body.Input)) {
		return
	}
	channel := firstChannel(route)
	if channel.Provider != "mock" {
		writeMediaPending(w, channel, requestID(r), body.Model, "audio.speech")
		return
	}
	setRouteHeaders(w, channel)
	format := body.ResponseFormat
	if format == "" {
		format = "mp3"
	}
	digest := mediaDigest(body.Model, body.Input, body.Voice, format)
	writeJSON(w, http.StatusOK, map[string]any{
		"object": "audio", "model": body.Model, "voice": body.Voice, "format": format,
		"audio_ref": "mock://audio/" + digest, "provider_execution": "mock",
	})
	s.recordMeter(r.Context(), metering.Record{RequestID: requestID(r), WorkspaceID: route.WorkspaceID, TokenID: route.TokenID, ChannelID: channel.ID, Model: body.Model, PromptTokens: estimateTokens(body.Input), StatusCode: http.StatusOK})
}

func (s *Server) audioTranscriptions(w http.ResponseWriter, r *http.Request) {
	var body audioTranscriptionRequest
	if err := decodeJSON(r.Body, &body); err != nil || strings.TrimSpace(body.Model) == "" || strings.TrimSpace(body.InputRef) == "" {
		writeOpenAIError(w, http.StatusBadRequest, "E00001", "model and input_ref are required")
		return
	}
	route, err := s.authenticate(r, body.Model)
	if err != nil {
		s.writeResolveError(w, err)
		return
	}
	if !s.enforceRateLimit(w, r, route, 1) {
		return
	}
	channel := firstChannel(route)
	if channel.Provider != "mock" {
		writeMediaPending(w, channel, requestID(r), body.Model, "audio.transcriptions")
		return
	}
	setRouteHeaders(w, channel)
	digest := mediaDigest(body.Model, body.InputRef, body.Language)
	writeJSON(w, http.StatusOK, map[string]any{
		"text": "WorkAMA local transcription " + digest[:12], "model": body.Model,
		"input_ref": body.InputRef, "language": body.Language, "provider_execution": "mock",
	})
	s.recordMeter(r.Context(), metering.Record{RequestID: requestID(r), WorkspaceID: route.WorkspaceID, TokenID: route.TokenID, ChannelID: channel.ID, Model: body.Model, PromptTokens: 1, StatusCode: http.StatusOK})
}

func (s *Server) renderMockImages(w http.ResponseWriter, r *http.Request, model, prompt, operation, source string, count int, size, responseFormat string) {
	route, err := s.authenticate(r, model)
	if err != nil {
		s.writeResolveError(w, err)
		return
	}
	if !s.enforceRateLimit(w, r, route, estimateTokens(prompt)) {
		return
	}
	channel := firstChannel(route)
	if channel.Provider != "mock" {
		writeMediaPending(w, channel, requestID(r), model, "images."+operation)
		return
	}
	setRouteHeaders(w, channel)
	if size == "" {
		size = "1024x1024"
	}
	if responseFormat == "" {
		responseFormat = "url"
	}
	data := make([]map[string]any, 0, count)
	for index := 0; index < count; index++ {
		digest := mediaDigest(model, operation, prompt, source, size, fmt.Sprint(index))
		item := map[string]any{
			"url": "mock://image/" + digest, "revised_prompt": prompt, "status": "completed", "index": index,
		}
		if responseFormat == "b64_json" {
			item["b64_json"] = hex.EncodeToString([]byte("workama-mock-image:" + digest))
			delete(item, "url")
		}
		data = append(data, item)
	}
	writeJSON(w, http.StatusOK, map[string]any{"created": time.Now().Unix(), "model": model, "data": data, "provider_execution": "mock", "operation": operation, "size": size})
	s.recordMeter(r.Context(), metering.Record{RequestID: requestID(r), WorkspaceID: route.WorkspaceID, TokenID: route.TokenID, ChannelID: channel.ID, Model: model, PromptTokens: estimateTokens(prompt), StatusCode: http.StatusOK})
}

func writeMediaPending(w http.ResponseWriter, channel relay.Channel, requestID, model, operation string) {
	setRouteHeaders(w, channel)
	writeJSON(w, http.StatusAccepted, map[string]any{
		"status": "pending_external", "provider_execution": "pending", "operation": operation,
		"request_id": requestID, "model": model,
	})
}

func mediaDigest(parts ...string) string {
	hash := sha256.Sum256([]byte(strings.Join(parts, "\x00")))
	return hex.EncodeToString(hash[:])
}
