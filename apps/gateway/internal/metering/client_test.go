package metering

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"
)

type fakePublisher struct {
	err       error
	subject   string
	messageID string
	payload   []byte
}

func (f *fakePublisher) Publish(_ context.Context, subject string, payload []byte, messageID string) error {
	f.subject = subject
	f.messageID = messageID
	f.payload = append([]byte(nil), payload...)
	return f.err
}

type fakeFallback struct {
	err   error
	calls int
	value Record
}

func (f *fakeFallback) Record(_ context.Context, value Record) error {
	f.calls++
	f.value = value
	return f.err
}

func fixedClient(publisher Publisher, fallback Recorder) *Client {
	return NewClientWithDependencies(
		publisher,
		fallback,
		func() time.Time { return time.Date(2026, 7, 14, 1, 2, 3, 0, time.UTC) },
		func() string { return "evt_fixed" },
	)
}

func sampleRecord() Record {
	tokenID := "gwt_fixed"
	return Record{
		RequestID: "req_fixed", WorkspaceID: "wsp_fixed", TokenID: &tokenID,
		ChannelID: "chn_fixed", Model: "workama-chat", PromptTokens: 12,
		CompletionTokens: 4, LatencyMS: 125, StatusCode: 200,
	}
}

func TestRecordPublishesFrozenMeteringEnvelope(t *testing.T) {
	publisher := &fakePublisher{}
	fallback := &fakeFallback{}
	client := fixedClient(publisher, fallback)

	if err := client.Record(context.Background(), sampleRecord()); err != nil {
		t.Fatalf("Record returned error: %v", err)
	}
	if publisher.subject != "metering.llm.v1" || publisher.messageID != "req_fixed" {
		t.Fatalf("unexpected publish metadata: %q %q", publisher.subject, publisher.messageID)
	}
	if fallback.calls != 0 {
		t.Fatalf("fallback called %d times", fallback.calls)
	}

	var event Event
	if err := json.Unmarshal(publisher.payload, &event); err != nil {
		t.Fatalf("decode event: %v", err)
	}
	if event.SchemaVersion != 1 || event.EventID != "evt_fixed" || event.EventType != "metering.llm.v1" {
		t.Fatalf("unexpected event identity: %+v", event)
	}
	if event.IdempotencyKey != "req_fixed" || event.Payload.Model != "workama-chat" {
		t.Fatalf("unexpected event payload: %+v", event)
	}
}

func TestRecordFallsBackToHTTPWhenPublishFails(t *testing.T) {
	publisher := &fakePublisher{err: errors.New("nats unavailable")}
	fallback := &fakeFallback{}
	client := fixedClient(publisher, fallback)

	if err := client.Record(context.Background(), sampleRecord()); err != nil {
		t.Fatalf("Record returned error: %v", err)
	}
	if fallback.calls != 1 || fallback.value.RequestID != "req_fixed" {
		t.Fatalf("unexpected fallback: %+v", fallback)
	}
}

func TestRecordReturnsErrorWhenPublishAndFallbackFail(t *testing.T) {
	client := fixedClient(
		&fakePublisher{err: errors.New("nats unavailable")},
		&fakeFallback{err: errors.New("platform unavailable")},
	)

	if err := client.Record(context.Background(), sampleRecord()); err == nil {
		t.Fatal("expected combined metering failure")
	}
}
