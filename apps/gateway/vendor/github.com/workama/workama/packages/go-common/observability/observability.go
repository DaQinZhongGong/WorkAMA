package observability

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.30.0"
	"go.opentelemetry.io/otel/trace"
)

type contextKey string

const requestIDKey contextKey = "wama-request-id"

var (
	requestIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`)
	initOnce sync.Once
	serviceName = "service"
	httpRequests metric.Int64Counter
	httpDuration metric.Float64Histogram
)

func ValidRequestID(value string) bool { return requestIDPattern.MatchString(value) }

func NewRequestID() string {
	value := make([]byte, 16)
	if _, err := rand.Read(value); err != nil { return fmt.Sprintf("req_%d", time.Now().UnixNano()) }
	return "req_" + hex.EncodeToString(value)
}

func RequestID(ctx context.Context) string {
	value, _ := ctx.Value(requestIDKey).(string)
	return value
}

func Init(ctx context.Context, service string) (func(context.Context) error, error) {
	var initErr error
	shutdown := func(context.Context) error { return nil }
	initOnce.Do(func() {
		serviceName = service
		res, err := resource.New(ctx, resource.WithAttributes(
			semconv.ServiceName(service), semconv.ServiceVersion(env("SERVICE_VERSION", "0.1.0")),
		))
		if err != nil { initErr = err; return }
		traceOptions := []sdktrace.TracerProviderOption{sdktrace.WithResource(res), sdktrace.WithSampler(sdktrace.AlwaysSample())}
		metricOptions := []sdkmetric.Option{sdkmetric.WithResource(res)}
		if enabled() {
			endpoint := strings.TrimPrefix(strings.TrimPrefix(env("OTEL_EXPORTER_OTLP_ENDPOINT", "otel-collector:4317"), "http://"), "https://")
			traceExporter, err := otlptracegrpc.New(ctx, otlptracegrpc.WithEndpoint(endpoint), otlptracegrpc.WithInsecure())
			if err != nil { initErr = err; return }
			metricExporter, err := otlpmetricgrpc.New(ctx, otlpmetricgrpc.WithEndpoint(endpoint), otlpmetricgrpc.WithInsecure())
			if err != nil { initErr = err; return }
			traceOptions = append(traceOptions, sdktrace.WithBatcher(traceExporter))
			metricOptions = append(metricOptions, sdkmetric.WithReader(sdkmetric.NewPeriodicReader(metricExporter, sdkmetric.WithInterval(5*time.Second))))
		}
		tracerProvider := sdktrace.NewTracerProvider(traceOptions...)
		meterProvider := sdkmetric.NewMeterProvider(metricOptions...)
		otel.SetTracerProvider(tracerProvider)
		otel.SetMeterProvider(meterProvider)
		otel.SetTextMapPropagator(propagation.TraceContext{})
		meter := otel.Meter(service)
		httpRequests, _ = meter.Int64Counter("wama_" + strings.ReplaceAll(service, "-", "_") + "_http_requests_total")
		httpDuration, _ = meter.Float64Histogram("wama_" + strings.ReplaceAll(service, "-", "_") + "_http_request_duration_seconds", metric.WithUnit("s"))
		shutdown = func(ctx context.Context) error {
			if err := meterProvider.Shutdown(ctx); err != nil { return err }
			return tracerProvider.Shutdown(ctx)
		}
	})
	return shutdown, initErr
}

type statusWriter struct { http.ResponseWriter; status int }
func (w *statusWriter) WriteHeader(status int) { w.status = status; w.ResponseWriter.WriteHeader(status) }
func (w *statusWriter) Unwrap() http.ResponseWriter { return w.ResponseWriter }
func (w *statusWriter) Flush() { _ = http.NewResponseController(w.ResponseWriter).Flush() }

func Middleware(service string, next http.Handler) http.Handler {
	_, _ = Init(context.Background(), service)
	return otelhttp.NewHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requestID := r.Header.Get("X-Wama-Request-ID")
		if requestID == "" { requestID = r.Header.Get("X-Request-ID") }
		if !ValidRequestID(requestID) { requestID = NewRequestID() }
		ctx := context.WithValue(r.Context(), requestIDKey, requestID)
		span := trace.SpanFromContext(ctx)
		span.SetAttributes(attribute.String("wama.request_id", requestID))
		w.Header().Set("X-Wama-Request-ID", requestID)
		w.Header().Set("X-Request-ID", requestID)
		carrier := propagation.HeaderCarrier(w.Header())
		otel.GetTextMapPropagator().Inject(ctx, carrier)
		started := time.Now()
		wrapped := &statusWriter{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(wrapped, r.WithContext(ctx))
		attrs := metric.WithAttributes(
			attribute.String("method", r.Method), attribute.String("route", route(r.URL.Path)),
			attribute.String("status_class", strconv.Itoa(wrapped.status/100)+"xx"),
		)
		if httpRequests != nil { httpRequests.Add(ctx, 1, attrs) }
		if httpDuration != nil { httpDuration.Record(ctx, time.Since(started).Seconds(), attrs) }
	}), service, otelhttp.WithMessageEvents(otelhttp.ReadEvents, otelhttp.WriteEvents))
}

func Transport(base http.RoundTripper) http.RoundTripper {
	if base == nil { base = http.DefaultTransport }
	return otelhttp.NewTransport(base)
}

func Logger(base *slog.Logger, ctx context.Context, workspaceID string) *slog.Logger {
	if base == nil { base = slog.Default() }
	spanContext := trace.SpanContextFromContext(ctx)
	traceID := ""
	if spanContext.IsValid() { traceID = spanContext.TraceID().String() }
	return base.With("service", serviceName, "trace_id", traceID, "request_id", RequestID(ctx), "org_id", "", "workspace_id", workspaceID)
}

func route(path string) string {
	switch path {
	case "/healthz", "/v1/models", "/v1/chat/completions", "/v1/embeddings": return path
	default: return "/unmatched"
	}
}

func enabled() bool {
	value := strings.ToLower(os.Getenv("OTEL_ENABLED"))
	return value == "1" || value == "true" || value == "yes"
}
func env(name, fallback string) string { if value := os.Getenv(name); value != "" { return value }; return fallback }
