package metering

import (
	"context"
	"fmt"
	"sync"
	"time"

	"github.com/nats-io/nats.go"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
)

type NATSPublisher struct {
	url  string
	mu   sync.Mutex
	conn *nats.Conn
	js   nats.JetStreamContext
}

type natsCarrier nats.Header
func (carrier natsCarrier) Get(key string) string { return nats.Header(carrier).Get(key) }
func (carrier natsCarrier) Set(key, value string) { nats.Header(carrier).Set(key, value) }
func (carrier natsCarrier) Keys() []string {
	keys := make([]string, 0, len(carrier))
	for key := range carrier { keys = append(keys, key) }
	return keys
}

func NewNATSPublisher(url string) *NATSPublisher {
	return &NATSPublisher{url: url}
}

func (p *NATSPublisher) Publish(ctx context.Context, subject string, payload []byte, messageID string) error {
	js, err := p.jetStream()
	if err != nil {
		return err
	}
	message := &nats.Msg{Subject: subject, Data: payload, Header: nats.Header{}}
	message.Header.Set(nats.MsgIdHdr, messageID)
	otel.GetTextMapPropagator().Inject(ctx, propagation.TextMapCarrier(natsCarrier(message.Header)))
	if _, err := js.PublishMsg(message, nats.Context(ctx)); err != nil {
		return fmt.Errorf("publish metering event: %w", err)
	}
	return nil
}

func (p *NATSPublisher) jetStream() (nats.JetStreamContext, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.conn != nil && p.conn.IsConnected() && p.js != nil {
		return p.js, nil
	}
	conn, err := nats.Connect(
		p.url,
		nats.Name("workama-gateway"),
		nats.Timeout(2*time.Second),
		nats.MaxReconnects(-1),
		nats.ReconnectWait(time.Second),
	)
	if err != nil {
		return nil, fmt.Errorf("connect nats: %w", err)
	}
	js, err := conn.JetStream(nats.PublishAsyncMaxPending(256))
	if err != nil {
		conn.Close()
		return nil, fmt.Errorf("open jetstream: %w", err)
	}
	p.conn = conn
	p.js = js
	return js, nil
}
