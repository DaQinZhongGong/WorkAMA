package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/workama/workama/apps/gateway/internal/metering"
	"github.com/workama/workama/apps/gateway/internal/observability"
	"github.com/workama/workama/apps/gateway/internal/relay"
	"github.com/workama/workama/apps/gateway/internal/relay/routing"
	_ "github.com/workama/workama/apps/gateway/internal/relay/adapter/anthropic"  // 注册 Anthropic 适配器工厂
	_ "github.com/workama/workama/apps/gateway/internal/relay/adapter/gemini"     // 注册 Gemini 适配器工厂
	_ "github.com/workama/workama/apps/gateway/internal/relay/adapter/openai"      // 注册 OpenAI 适配器工厂
	"github.com/workama/workama/apps/gateway/internal/server"
	"github.com/workama/workama/apps/gateway/internal/server/httpapi"
	"github.com/workama/workama/apps/gateway/internal/server/middleware"
	"github.com/workama/workama/apps/gateway/internal/store/pg"
)

func main() {
	shutdown, err := observability.Init(context.Background(), "gateway")
	if err != nil {
		slog.Error("initialize observability", "error", err)
	}
	defer func() { _ = shutdown(context.Background()) }()
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil)).With(
		"service", "gateway", "trace_id", "", "request_id", "",
		"org_id", "", "workspace_id", "",
	)
	platformURL := env("PLATFORM_API_URL", "http://localhost:8000")
	internalToken, err := resolveInternalToken()
	if err != nil {
		logger.Error("internal token configuration rejected", "error", err)
		os.Exit(1)
	}
	natsURL := env("NATS_URL", "nats://localhost:4222")
	port := env("PORT", "8080")

	service := server.New(
		relay.NewPlatformClient(platformURL, internalToken),
		metering.NewClient(metering.NewNATSPublisher(natsURL), platformURL, internalToken),
		internalToken,
		logger,
	)

	// 尝试初始化 pg 直连管道（10 步管道的 Go 实现）。
	// DATABASE_URL 未设置或 pg 连接失败时退回 Python relay 后端。
	wireDirectPipeline(service, logger, internalToken)

	httpServer := &http.Server{
		Addr:              ":" + port,
		Handler:           service.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout:       90 * time.Second,
	}
	logger.Info("gateway listening", "port", port)
	if err := httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		logger.Error("gateway stopped", "error", err)
		os.Exit(1)
	}
}

// wireDirectPipeline 在 DATABASE_URL 配置时初始化 pg 直连管道并注入到 service。
// 完整 10 步中间件管道：
//
//	①认证 → ②授权 → ③限流 → ④预算 → ⑤输入审查
//	→ ⑥模型映射 → ⑦路由 → ⑧转发 → ⑨输出审查 → ⑩计量
//
// AuthMiddleware 负责 ①认证；ChatHandler 内部完成 ②授权/⑤输入审查/⑨输出审查；
// RateLimitMiddleware 负责 ③限流；BudgetMiddleware 负责 ④预算；
// ChatHandler 完成 ⑥模型映射 → ⑦路由 → ⑧转发 → ⑩计量。
func wireDirectPipeline(service *server.Server, logger *slog.Logger, internalToken string) {
	databaseURL := os.Getenv("DATABASE_URL")
	if databaseURL == "" {
		logger.Info("DATABASE_URL not set, using Python relay backend for chat completions")
		return
	}
	pepper := env("KEY_PEPPER", "change-this-pepper")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	pgGateway, err := pg.New(ctx, databaseURL, pepper, env("ENCRYPTION_KEY", ""))
	if err != nil {
		logger.Error("pg gateway init failed, falling back to Python relay backend",
			"error", err)
		return
	}

	// 路由组件：⑦路由（weighted 加权随机）+ 渠道级熔断
	breaker := routing.NewCircuitBreaker()
	router := routing.NewWeightedRouter(nil)

	// 中间件：①认证 + ③限流 + ④预算 + ⑩计量
	authMW := middleware.NewAuth(pgGateway, internalToken) // pgGateway 实现 token.Verifier；INTERNAL_TOKEN 仅从环境注入
	limiter := server.NewLimiter()
	rateLimitMW := middleware.NewRateLimit(limiter)
	budgetMW := middleware.NewBudget(nil) // ④预算：Balance=nil 视为放行（后续接入 bill_account）
	meterMW := middleware.NewMeter(service.Metering, logger)

	// 请求日志写入器：适配 pg.Gateway 到 httpapi.RequestLogWriter
	logWriter := &pgLogWriter{pg: pgGateway}

	// ChatHandler：⑥模型映射 → ⑦路由 → ⑧转发 → ⑩计量
	chatHandler := httpapi.NewChat(pgGateway, logWriter, breaker, router, meterMW, logger)

	// ModelsHandler：聚合工作空间下所有渠道的模型列表
	modelsHandler := &httpapi.ModelsHandler{Channels: pgGateway}

	// 中间件链：①认证 → ③限流 → ④预算 → handler
	service.ChatHandlerOverride = authMW.Wrap(rateLimitMW.Wrap(budgetMW.Wrap(chatHandler)))
	service.ModelsHandlerOverride = authMW.Wrap(modelsHandler)

	// /healthz 检查 pg 连接池健康
	service.HealthChecker = pgGateway

	logger.Info("direct pg pipeline enabled for /v1/chat/completions and /v1/models")
}

// pgLogWriter 适配 store/pg.Gateway 到 httpapi.RequestLogWriter 接口。
// 将 httpapi.RequestLogRow 转换为 pg.RequestLog 并写入 gw_request_log。
type pgLogWriter struct {
	pg *pg.Gateway
}

// InsertRequestLog implements httpapi.RequestLogWriter.
func (w *pgLogWriter) InsertRequestLog(ctx context.Context, row *httpapi.RequestLogRow) error {
	if row == nil {
		return nil
	}
	return w.pg.InsertRequestLog(ctx, &pg.RequestLog{
		RequestID:        row.RequestID,
		WorkspaceID:     row.WorkspaceID,
		TokenID:         row.TokenID,
		ChannelID:       row.ChannelID,
		Model:           row.Model,
		PromptTokens:    row.PromptTokens,
		CompletionTokens: row.CompletionTokens,
		LatencyMS:       row.LatencyMS,
		StatusCode:      row.StatusCode,
		ErrorCode:       row.ErrorCode,
	})
}

func env(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

// knownPlaceholderInternalTokens are documented fill-me values. They must
// never be accepted as a live INTERNAL_TOKEN.
var knownPlaceholderInternalTokens = map[string]struct{}{
	"change-this-internal-token": {},
}

// knownDevInternalTokens are documented local/dev defaults. Accepted only
// when WORKAMA_ENV is not production/prod.
var knownDevInternalTokens = map[string]struct{}{
	"workama-dev-internal-token-2026":                       {},
	"workama-local-internal-token-change-before-production": {},
}

func isProductionEnv(value string) bool {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "production", "prod":
		return true
	default:
		return false
	}
}

// resolveInternalToken reads INTERNAL_TOKEN from the process environment.
// There is no source-code fallback: an unset, empty, or documented
// placeholder token rejects startup. Known development defaults are
// allowed only outside production.
func resolveInternalToken() (string, error) {
	value := strings.TrimSpace(os.Getenv("INTERNAL_TOKEN"))
	if value == "" {
		return "", errors.New("INTERNAL_TOKEN is required and must be injected via environment")
	}
	if _, ok := knownPlaceholderInternalTokens[value]; ok {
		return "", errors.New("INTERNAL_TOKEN is a documented placeholder; set a unique secret")
	}
	if isProductionEnv(os.Getenv("WORKAMA_ENV")) {
		if _, ok := knownDevInternalTokens[value]; ok {
			return "", errors.New("INTERNAL_TOKEN is a known development default and is rejected when WORKAMA_ENV=production")
		}
	}
	return value, nil
}
