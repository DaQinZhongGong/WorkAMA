// Package stream provides SSE passthrough and usage aggregation for the
// relay pipeline. It reads a streaming upstream response line by line,
// forwards each SSE event to the client, and aggregates token usage.
package stream

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/workama/workama/apps/gateway/internal/relay/adapter"
)

// AggregatedUsage accumulates prompt / completion tokens across SSE chunks.
type AggregatedUsage struct {
	mu               sync.Mutex
	promptTokens     int
	completionTokens int
	totalTokens      int
	chunksSeen       int
	firstByteAt      time.Time
}

// Aggregate returns the aggregated usage snapshot.
func (a *AggregatedUsage) Aggregate() *adapter.Usage {
	a.mu.Lock()
	defer a.mu.Unlock()
	return &adapter.Usage{
		PromptTokens:     a.promptTokens,
		CompletionTokens: a.completionTokens,
		TotalTokens:      a.totalTokens,
	}
}

// ChunksSeen returns how many SSE chunks were processed.
func (a *AggregatedUsage) ChunksSeen() int {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.chunksSeen
}

// FirstByteAt returns the time of the first chunk processed.
func (a *AggregatedUsage) FirstByteAt() time.Time {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.firstByteAt
}

// merge merges a chunk's usage into the aggregator (under lock).
func (a *AggregatedUsage) merge(usage *adapter.Usage) {
	if usage == nil {
		return
	}
	if usage.PromptTokens > a.promptTokens {
		a.promptTokens = usage.PromptTokens
	}
	a.completionTokens += usage.CompletionTokens
	if usage.TotalTokens > a.totalTokens {
		a.totalTokens = usage.TotalTokens
	}
	if a.totalTokens == 0 && (a.promptTokens > 0 || a.completionTokens > 0) {
		a.totalTokens = a.promptTokens + a.completionTokens
	}
}

// PassthroughOptions configures the SSE passthrough.
type PassthroughOptions struct {
	Adapter          adapter.Adapter
	Model            string
	FirstByteTimeout time.Duration
	FlushInterval    time.Duration
}

// ErrUpstreamTimeout is returned when the upstream never sends a chunk.
var ErrUpstreamTimeout = errors.New("upstream stream timed out before first chunk")

// Passthrough forwards upstream SSE to the client line by line.
// It writes `data: <payload>\n\n` per event and a final `data: [DONE]\n\n`
// if the upstream did not send one. The aggregator is updated as chunks arrive.
//
// 该函数不关闭 upstream 或 destination；调用方负责资源管理。
func Passthrough(ctx context.Context, upstream io.Reader, destination io.Writer, usage *AggregatedUsage, opts PassthroughOptions) error {
	if upstream == nil {
		return fmt.Errorf("upstream reader is nil")
	}
	if destination == nil {
		return fmt.Errorf("destination writer is nil")
	}
	if usage == nil {
		usage = &AggregatedUsage{}
	}
	if opts.FirstByteTimeout <= 0 {
		opts.FirstByteTimeout = 15 * time.Second
	}
	scanner := bufio.NewScanner(upstream)
	scanner.Buffer(make([]byte, 0, 64<<10), 1<<20)
	flusher, _ := destination.(http.Flusher)
	started := false
	done := false
	eventLines := []string{}
	var firstEventStarted bool
	var firstEventMu sync.Mutex
	timer := time.AfterFunc(opts.FirstByteTimeout, func() {
		firstEventMu.Lock()
		defer firstEventMu.Unlock()
		if !firstEventStarted {
			if closer, ok := upstream.(interface{ Close() error }); ok {
				_ = closer.Close()
			}
		}
	})
	defer timer.Stop()
	flushEvent := func() error {
		if len(eventLines) == 0 {
			return nil
		}
		lines, hasDone, payload := parseSSEEvent(eventLines)
		eventLines = nil
		if hasDone {
			done = true
		}
		if !started {
			if payload == "" {
				return nil
			}
			firstEventMu.Lock()
			firstEventStarted = true
			firstEventMu.Unlock()
			timer.Stop()
			usage.firstByteAt = time.Now()
			started = true
		}
		if opts.Adapter != nil && payload != "" && payload != "[DONE]" {
			if chunk, err := opts.Adapter.ParseStreamChunk([]byte(payload)); err == nil && chunk != nil {
				usage.merge(chunk.Usage)
				usage.mu.Lock()
				usage.chunksSeen++
				usage.mu.Unlock()
			}
		}
		for _, line := range lines {
			if _, err := fmt.Fprintln(destination, line); err != nil {
				return err
			}
		}
		if _, err := fmt.Fprintln(destination); err != nil {
			return err
		}
		if flusher != nil {
			flusher.Flush()
		}
		return nil
	}
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			if err := flushEvent(); err != nil {
				return err
			}
			continue
		}
		eventLines = append(eventLines, line)
	}
	if err := scanner.Err(); err != nil {
		return err
	}
	if err := flushEvent(); err != nil {
		return err
	}
	if !started {
		return ErrUpstreamTimeout
	}
	if !done {
		if _, err := fmt.Fprint(destination, "data: [DONE]\n\n"); err != nil {
			return err
		}
		if flusher != nil {
			flusher.Flush()
		}
	}
	return nil
}

// parseSSEEvent extracts the data payload from accumulated SSE event lines.
// 返回 (lines_to_emit, is_done, data_payload)。
func parseSSEEvent(lines []string) ([]string, bool, string) {
	hasDone := false
	dataParts := []string{}
	hasData := false
	for _, line := range lines {
		payload, ok := sseDataPayload(line)
		if !ok {
			continue
		}
		hasData = true
		if strings.TrimSpace(payload) == "[DONE]" {
			hasDone = true
			continue
		}
		dataParts = append(dataParts, payload)
	}
	if !hasData {
		return lines, hasDone, ""
	}
	return lines, hasDone, strings.Join(dataParts, "\n")
}

func sseDataPayload(line string) (string, bool) {
	if !strings.HasPrefix(line, "data:") {
		return "", false
	}
	payload := strings.TrimPrefix(line, "data:")
	if strings.HasPrefix(payload, " ") {
		payload = payload[1:]
	}
	return payload, true
}

// SynthesizeStream wraps a non-streaming OpenAI-compatible response into a
// single-chunk SSE stream + [DONE] marker. 与 Python _synthesize_stream 一致。
func SynthesizeStream(payload []byte) ([]byte, error) {
	var resp map[string]any
	if err := json.Unmarshal(payload, &resp); err != nil {
		return nil, fmt.Errorf("synthesize stream: decode payload: %w", err)
	}
	choices, _ := resp["choices"].([]any)
	content := ""
	finish := "stop"
	if len(choices) > 0 {
		if choice, ok := choices[0].(map[string]any); ok {
			if msg, ok := choice["message"].(map[string]any); ok {
				if text, _ := msg["content"].(string); text != "" {
					content = text
				}
			}
			if reason, _ := choice["finish_reason"].(string); reason != "" {
				finish = reason
			}
		}
	}
	id, _ := resp["id"].(string)
	if id == "" {
		id = "chatcmpl-workama"
	}
	model, _ := resp["model"].(string)
	created := time.Now().Unix()
	if c, ok := resp["created"].(float64); ok {
		created = int64(c)
	}
	first := map[string]any{
		"id":      id,
		"object":  "chat.completion.chunk",
		"created": created,
		"model":   model,
		"choices": []map[string]any{{
			"index":         0,
			"delta":         map[string]any{"role": "assistant", "content": content},
			"finish_reason": nil,
		}},
	}
	second := map[string]any{
		"id":      id,
		"object":  "chat.completion.chunk",
		"created": created,
		"model":   model,
		"choices": []map[string]any{{
			"index":         0,
			"delta":         map[string]any{},
			"finish_reason": finish,
		}},
	}
	firstBytes, _ := json.Marshal(first)
	secondBytes, _ := json.Marshal(second)
	out := "data: " + string(firstBytes) + "\n\n"
	out += "data: " + string(secondBytes) + "\n\n"
	out += "data: [DONE]\n\n"
	return []byte(out), nil
}
