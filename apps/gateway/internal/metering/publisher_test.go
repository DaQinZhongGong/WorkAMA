package metering

import (
	"context"
	"errors"
	"reflect"
	"strings"
	"sync"
	"testing"
	"unsafe"

	"github.com/nats-io/nats.go"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
)

// --- Mock JetStream ---

// mockJetStream implements nats.JetStreamContext by embedding the interface
// (nil) and overriding only PublishMsg — the sole method exercised by
// NATSPublisher.Publish. Calling any other method panics with a nil
// pointer dereference, which is acceptable for these tests.
type mockJetStream struct {
	nats.JetStreamContext
	mu              sync.Mutex
	publishMsgErr   error
	publishMsgCalls []mockPublishMsgCall
}

type mockPublishMsgCall struct {
	Subject string
	Data    []byte
	MsgID   string
	Header  nats.Header
}

func (m *mockJetStream) PublishMsg(msg *nats.Msg, opts ...nats.PubOpt) (*nats.PubAck, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.publishMsgCalls = append(m.publishMsgCalls, mockPublishMsgCall{
		Subject: msg.Subject,
		Data:    append([]byte(nil), msg.Data...),
		MsgID:   msg.Header.Get(nats.MsgIdHdr),
		Header:  msg.Header,
	})
	if m.publishMsgErr != nil {
		return nil, m.publishMsgErr
	}
	return &nats.PubAck{Stream: "METERING", Sequence: uint64(len(m.publishMsgCalls))}, nil
}

// forceConnected sets the unexported status field of *nats.Conn to CONNECTED
// so that NATSPublisher.jetStream() returns the injected mock instead of
// dialing a real NATS server. This avoids the need for a live NATS instance
// while still exercising the full Publish code path.
func forceConnected(conn *nats.Conn) {
	statusField := reflect.ValueOf(conn).Elem().FieldByName("status")
	*(*nats.Status)(unsafe.Pointer(statusField.UnsafeAddr())) = nats.CONNECTED
}

// newPublisherWithMock builds a NATSPublisher whose jetStream() returns the
// provided mock without attempting a real connection.
func newPublisherWithMock(js *mockJetStream) *NATSPublisher {
	conn := &nats.Conn{}
	forceConnected(conn)
	return &NATSPublisher{url: "nats://unused:4222", conn: conn, js: js}
}

// --- Constructor ---

func TestNATSPublisherConstructorStoresURL(t *testing.T) {
	publisher := NewNATSPublisher("nats://broker:4222")
	if publisher == nil {
		t.Fatal("NewNATSPublisher returned nil")
	}
	if publisher.url != "nats://broker:4222" {
		t.Fatalf("url = %q, want %q", publisher.url, "nats://broker:4222")
	}
	if publisher.conn != nil || publisher.js != nil {
		t.Fatal("constructor should not eagerly connect")
	}
}

// --- Publish happy path ---

func TestNATSPublisherPublishSendsSubjectPayloadAndMessageID(t *testing.T) {
	js := &mockJetStream{}
	publisher := newPublisherWithMock(js)

	payload := []byte(`{"event":"metering"}`)
	if err := publisher.Publish(context.Background(), "metering.llm.v1", payload, "req_001"); err != nil {
		t.Fatalf("Publish returned error: %v", err)
	}

	js.mu.Lock()
	defer js.mu.Unlock()
	if len(js.publishMsgCalls) != 1 {
		t.Fatalf("publishMsgCalls = %d, want 1", len(js.publishMsgCalls))
	}
	call := js.publishMsgCalls[0]
	if call.Subject != "metering.llm.v1" {
		t.Fatalf("subject = %q, want %q", call.Subject, "metering.llm.v1")
	}
	if string(call.Data) != `{"event":"metering"}` {
		t.Fatalf("data = %q", string(call.Data))
	}
	if call.MsgID != "req_001" {
		t.Fatalf("message ID = %q, want %q", call.MsgID, "req_001")
	}
}

func TestNATSPublisherPublishDoesNotMutateCallerPayload(t *testing.T) {
	js := &mockJetStream{}
	publisher := newPublisherWithMock(js)

	payload := []byte(`{"original":true}`)
	if err := publisher.Publish(context.Background(), "metering.llm.v1", payload, "req_002"); err != nil {
		t.Fatalf("Publish returned error: %v", err)
	}
	if string(payload) != `{"original":true}` {
		t.Fatalf("caller payload was mutated: %q", string(payload))
	}
}

func TestNATSPublisherPublishSetsNatsMsgIdHeader(t *testing.T) {
	js := &mockJetStream{}
	publisher := newPublisherWithMock(js)

	if err := publisher.Publish(context.Background(), "metering.llm.v1", []byte("data"), "req_003"); err != nil {
		t.Fatalf("Publish returned error: %v", err)
	}

	js.mu.Lock()
	defer js.mu.Unlock()
	if len(js.publishMsgCalls) != 1 {
		t.Fatalf("publishMsgCalls = %d, want 1", len(js.publishMsgCalls))
	}
	header := js.publishMsgCalls[0].Header
	if header.Get(nats.MsgIdHdr) != "req_003" {
		t.Fatalf("Nats-Msg-Id header = %q, want %q", header.Get(nats.MsgIdHdr), "req_003")
	}
}

// TestNATSPublisherPublishInjectsTraceContext verifies that OTel trace
// context is injected into the message headers via the global propagator.
func TestNATSPublisherPublishInjectsTraceContext(t *testing.T) {
	// Save and restore the global propagator so this test does not leak state.
	previous := otel.GetTextMapPropagator()
	otel.SetTextMapPropagator(propagation.TraceContext{})
	defer otel.SetTextMapPropagator(previous)

	js := &mockJetStream{}
	publisher := newPublisherWithMock(js)

	traceID := trace.TraceID{0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
		0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f, 0x10}
	spanID := trace.SpanID{0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18}
	ctx := trace.ContextWithSpanContext(context.Background(),
		trace.NewSpanContext(trace.SpanContextConfig{
			TraceID:    traceID,
			SpanID:     spanID,
			TraceFlags: trace.FlagsSampled,
		}))

	if err := publisher.Publish(ctx, "metering.llm.v1", []byte("payload"), "req_trace"); err != nil {
		t.Fatalf("Publish returned error: %v", err)
	}

	js.mu.Lock()
	defer js.mu.Unlock()
	if len(js.publishMsgCalls) != 1 {
		t.Fatalf("publishMsgCalls = %d, want 1", len(js.publishMsgCalls))
	}
	tp := js.publishMsgCalls[0].Header.Get("traceparent")
	if tp == "" {
		t.Fatal("traceparent header was not injected")
	}
	expectedPrefix := "00-0102030405060708090a0b0c0d0e0f10-"
	if !strings.HasPrefix(tp, expectedPrefix) {
		t.Fatalf("traceparent = %q, want prefix %q", tp, expectedPrefix)
	}
}

// --- Publish error path (mock returns error) ---

func TestNATSPublisherPublishWrapsJetStreamError(t *testing.T) {
	js := &mockJetStream{publishMsgErr: errors.New("stream full")}
	publisher := newPublisherWithMock(js)

	err := publisher.Publish(context.Background(), "metering.llm.v1", []byte("data"), "req_err")
	if err == nil {
		t.Fatal("expected error from Publish")
	}
	if !strings.Contains(err.Error(), "publish metering event") {
		t.Fatalf("error = %v, want wrapper containing 'publish metering event'", err)
	}
	if !strings.Contains(err.Error(), "stream full") {
		t.Fatalf("error = %v, want wrapped error containing 'stream full'", err)
	}
}

// --- Publish error path (unreachable URL) ---

func TestNATSPublisherPublishFailsOnUnreachableURL(t *testing.T) {
	publisher := NewNATSPublisher("nats://127.0.0.1:1")

	err := publisher.Publish(context.Background(), "metering.llm.v1", []byte("data"), "req_unreachable")
	if err == nil {
		t.Fatal("expected error for unreachable NATS URL")
	}
	if !strings.Contains(err.Error(), "connect nats") {
		t.Fatalf("error = %v, want wrapper containing 'connect nats'", err)
	}
}

// --- Reuses cached connection ---

func TestNATSPublisherReusesCachedJetStream(t *testing.T) {
	js := &mockJetStream{}
	publisher := newPublisherWithMock(js)

	// First publish should use the injected mock.
	if err := publisher.Publish(context.Background(), "metering.llm.v1", []byte("first"), "req_a"); err != nil {
		t.Fatalf("first publish error: %v", err)
	}
	// Second publish should reuse the same mock (no reconnection).
	if err := publisher.Publish(context.Background(), "metering.llm.v1", []byte("second"), "req_b"); err != nil {
		t.Fatalf("second publish error: %v", err)
	}

	js.mu.Lock()
	defer js.mu.Unlock()
	if len(js.publishMsgCalls) != 2 {
		t.Fatalf("publishMsgCalls = %d, want 2", len(js.publishMsgCalls))
	}
	if js.publishMsgCalls[0].MsgID != "req_a" || js.publishMsgCalls[1].MsgID != "req_b" {
		t.Fatalf("call order = %q %q", js.publishMsgCalls[0].MsgID, js.publishMsgCalls[1].MsgID)
	}
}

// --- Concurrent safety ---

func TestNATSPublisherConcurrentPublishIsSafe(t *testing.T) {
	js := &mockJetStream{}
	publisher := newPublisherWithMock(js)

	const goroutines = 50
	var wg sync.WaitGroup
	for i := 0; i < goroutines; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			err := publisher.Publish(context.Background(), "metering.llm.v1",
				[]byte("payload"), "req_concurrent")
			if err != nil {
				t.Errorf("concurrent publish error: %v", err)
			}
		}()
	}
	wg.Wait()

	js.mu.Lock()
	defer js.mu.Unlock()
	if len(js.publishMsgCalls) != goroutines {
		t.Fatalf("publishMsgCalls = %d, want %d", len(js.publishMsgCalls), goroutines)
	}
}

// --- natsCarrier tests ---

func TestNatsCarrierGetReturnsEmptyForMissingKey(t *testing.T) {
	carrier := natsCarrier(nats.Header{})
	if got := carrier.Get("missing"); got != "" {
		t.Fatalf("Get(missing) = %q, want empty", got)
	}
}

func TestNatsCarrierSetAndGetRoundTrip(t *testing.T) {
	carrier := natsCarrier(nats.Header{})
	carrier.Set("key1", "value1")
	carrier.Set("key2", "value2")
	if got := carrier.Get("key1"); got != "value1" {
		t.Fatalf("Get(key1) = %q, want %q", got, "value1")
	}
	if got := carrier.Get("key2"); got != "value2" {
		t.Fatalf("Get(key2) = %q, want %q", got, "value2")
	}
	// Set overwrites existing value.
	carrier.Set("key1", "updated")
	if got := carrier.Get("key1"); got != "updated" {
		t.Fatalf("Get(key1) after overwrite = %q, want %q", got, "updated")
	}
}

func TestNatsCarrierKeysReturnsAllKeys(t *testing.T) {
	carrier := natsCarrier(nats.Header{})
	carrier.Set("alpha", "1")
	carrier.Set("beta", "2")
	carrier.Set("gamma", "3")

	keys := carrier.Keys()
	if len(keys) != 3 {
		t.Fatalf("Keys() returned %d keys, want 3", len(keys))
	}

	// Keys come from a map, so order is non-deterministic.
	keySet := make(map[string]bool, len(keys))
	for _, k := range keys {
		keySet[k] = true
	}
	for _, expected := range []string{"alpha", "beta", "gamma"} {
		if !keySet[expected] {
			t.Fatalf("Keys() missing %q: %v", expected, keys)
		}
	}
}

func TestNatsCarrierKeysReturnsEmptyForEmptyCarrier(t *testing.T) {
	carrier := natsCarrier(nats.Header{})
	keys := carrier.Keys()
	if len(keys) != 0 {
		t.Fatalf("Keys() on empty carrier returned %d keys, want 0", len(keys))
	}
}

func TestNatsCarrierOTelPropagatorRoundTrip(t *testing.T) {
	propagator := propagation.TraceContext{}
	carrier := natsCarrier(nats.Header{})

	traceID := trace.TraceID{1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16}
	spanID := trace.SpanID{17, 18, 19, 20, 21, 22, 23, 24}
	ctx := trace.ContextWithSpanContext(context.Background(),
		trace.NewSpanContext(trace.SpanContextConfig{
			TraceID:    traceID,
			SpanID:     spanID,
			TraceFlags: trace.FlagsSampled,
		}))

	// Inject trace context into the carrier.
	propagator.Inject(ctx, carrier)

	// Extract trace context from the carrier.
	extracted := propagator.Extract(context.Background(), carrier)
	extractedSC := trace.SpanContextFromContext(extracted)
	if !extractedSC.IsValid() {
		t.Fatal("extracted span context is invalid")
	}
	if extractedSC.TraceID() != traceID {
		t.Fatalf("extracted TraceID = %s, want %s", extractedSC.TraceID(), traceID)
	}
	if extractedSC.SpanID() != spanID {
		t.Fatalf("extracted SpanID = %s, want %s", extractedSC.SpanID(), spanID)
	}
}
