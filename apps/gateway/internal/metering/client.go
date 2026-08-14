package metering

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	commonobservability "github.com/workama/workama/packages/go-common/observability"
)

type Record struct {
	RequestID        string  `json:"request_id"`
	WorkspaceID      string  `json:"workspace_id"`
	TokenID          *string `json:"token_id"`
	ChannelID        string  `json:"channel_id"`
	Model            string  `json:"model"`
	PromptTokens     int     `json:"prompt_tokens"`
	CompletionTokens int     `json:"completion_tokens"`
	LatencyMS        int64   `json:"latency_ms"`
	StatusCode       int     `json:"status_code"`
	ErrorCode        string  `json:"error_code,omitempty"`
}

type Event struct {
	SchemaVersion  int       `json:"schema_version"`
	EventID        string    `json:"event_id"`
	EventType      string    `json:"event_type"`
	OccurredAt     time.Time `json:"occurred_at"`
	Producer       string    `json:"producer"`
	WorkspaceID    string    `json:"workspace_id"`
	TraceID        string    `json:"trace_id"`
	IdempotencyKey string    `json:"idempotency_key"`
	Classification string    `json:"classification"`
	Payload        Record    `json:"payload"`
}

type Publisher interface {
	Publish(ctx context.Context, subject string, payload []byte, messageID string) error
}

type Recorder interface {
	Record(ctx context.Context, value Record) error
}

type Client struct {
	Publisher  Publisher
	Fallback   Recorder
	Now        func() time.Time
	NewEventID func() string
}

type httpRecorder struct {
	endpoint      string
	internalToken string
	http          *http.Client
}

func NewClient(publisher Publisher, platformURL, internalToken string) *Client {
	return NewClientWithDependencies(
		publisher,
		&httpRecorder{
			endpoint:      strings.TrimRight(platformURL, "/") + "/internal/gateway/meter",
			internalToken: internalToken,
			http:          &http.Client{Timeout: 5 * time.Second, Transport: commonobservability.Transport(nil)},
		},
		time.Now,
		newEventID,
	)
}

func NewClientWithDependencies(publisher Publisher, fallback Recorder, now func() time.Time, newID func() string) *Client {
	return &Client{
		Publisher: publisher, Fallback: fallback, Now: now, NewEventID: newID,
	}
}

func (c *Client) Record(ctx context.Context, value Record) error {
	event := Event{
		SchemaVersion: 1, EventID: c.NewEventID(), EventType: "metering.llm.v1",
		OccurredAt: c.Now().UTC(), Producer: "gateway", WorkspaceID: value.WorkspaceID,
		TraceID: value.RequestID, IdempotencyKey: value.RequestID, Classification: "C2",
		Payload: value,
	}
	payload, err := json.Marshal(event)
	if err != nil {
		return err
	}
	if c.Publisher != nil {
		if err := c.Publisher.Publish(ctx, "metering.llm.v1", payload, value.RequestID); err == nil {
			return nil
		}
	}
	if c.Fallback == nil {
		return fmt.Errorf("metering publisher failed and no fallback is configured")
	}
	if err := c.Fallback.Record(ctx, value); err != nil {
		return fmt.Errorf("metering publish and fallback failed: %w", err)
	}
	return nil
}

func (c *httpRecorder) Record(ctx context.Context, value Record) error {
	body, err := json.Marshal(value)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.endpoint, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Internal-Token", c.internalToken)
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		payload, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("metering failed with %d: %s", resp.StatusCode, payload)
	}
	return nil
}

func newEventID() string {
	value := make([]byte, 16)
	if _, err := rand.Read(value); err != nil {
		return fmt.Sprintf("evt_%d", time.Now().UnixNano())
	}
	return fmt.Sprintf("evt_%x", value)
}
