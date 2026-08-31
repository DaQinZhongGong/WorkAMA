package main

import (
	"context"
	"encoding/base64"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"

	stagingAdapter "github.com/workama/workama/apps/gateway/internal/adapter"
	"github.com/workama/workama/apps/gateway/internal/configsync"
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
	pepper, err := resolveKeyPepper()
	if err != nil {
		logger.Error("key pepper configuration rejected", "error", err)
		os.Exit(1)
	}
	encryptionKey, err := resolveEncryptionKey()
	if err != nil {
		logger.Error("encryption key configuration rejected", "error", err)
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
	chatHandler := wireDirectPipeline(service, logger, internalToken, pepper, encryptionKey)

	// 配置中心热下发：轮询 /internal/config/export，把 UI 发布的
	// llm_staging_* 覆盖渠道解密并热应用到 ChatHandler。仅在 pg 直连管道
	// 激活时有意义（relay 路径由 Python 侧自行消费配置中心）。
	if chatHandler != nil {
		startConfigSync(chatHandler, configSyncConfig{
			endpoint:      platformURL + "/internal/config/export",
			token:         internalToken,
			encryptionKey: encryptionKey,
			logger:        logger,
		})
	}

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

// wireDirectPipeline 在 DATABASE_URL 配置时初始化 pg 直连管道并注入到 service，
// 返回 ChatHandler（供配置中心热下发 staging 覆盖渠道）；未启用时返回 nil。
// 完整 10 步中间件管道：
//
//	①认证 → ②授权 → ③限流 → ④预算 → ⑤输入审查
//	→ ⑥模型映射 → ⑦路由 → ⑧转发 → ⑨输出审查 → ⑩计量
//
// AuthMiddleware 负责 ①认证；ChatHandler 内部完成 ②授权/⑤输入审查/⑨输出审查；
// RateLimitMiddleware 负责 ③限流；BudgetMiddleware 负责 ④预算；
// ChatHandler 完成 ⑥模型映射 → ⑦路由 → ⑧转发 → ⑩计量。
func wireDirectPipeline(service *server.Server, logger *slog.Logger, internalToken, pepper, encryptionKey string) *httpapi.ChatHandler {
	databaseURL := os.Getenv("DATABASE_URL")
	if databaseURL == "" {
		logger.Info("DATABASE_URL not set, using Python relay backend for chat completions")
		return nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	pgGateway, err := pg.New(ctx, databaseURL, pepper, encryptionKey)
	if err != nil {
		logger.Error("pg gateway init failed, falling back to Python relay backend",
			"error", err)
		return nil
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
	// 真实 LLM 覆盖渠道（LLM_STAGING_*）：优先于 DB 渠道，失败回退。
	if staging := stagingAdapter.LoadStagingConfig(); staging != nil && staging.Enabled {
		chatHandler.SetStaging(&httpapi.StagingChannel{Provider: staging.Provider, BaseURL: staging.BaseURL, APIKey: staging.APIKey, Model: staging.Model})
	}

	// ModelsHandler：聚合工作空间下所有渠道的模型列表
	modelsHandler := &httpapi.ModelsHandler{Channels: pgGateway}

	// 中间件链：①认证 → ③限流 → ④预算 → handler
	service.ChatHandlerOverride = authMW.Wrap(rateLimitMW.Wrap(budgetMW.Wrap(chatHandler)))
	service.ModelsHandlerOverride = authMW.Wrap(modelsHandler)

	// /healthz 检查 pg 连接池健康
	service.HealthChecker = pgGateway

	logger.Info("direct pg pipeline enabled for /v1/chat/completions and /v1/models")
	return chatHandler
}

// configSyncConfig 汇总配置中心轮询所需参数。
type configSyncConfig struct {
	endpoint      string
	token         string
	encryptionKey string
	logger        *slog.Logger
}

// startConfigSync 启动后台轮询：UI 发布 llm_staging_* 后 ≤interval 秒内
// 热应用（enabled=false 或必填缺失时清除覆盖，回退 DB 渠道路由）。
// UI 值优先级最高——快照到达后覆盖 LLM_STAGING_* env 的启动注入。
func startConfigSync(handler *httpapi.ChatHandler, cfg configSyncConfig) {
	poller := &configsync.Poller{
		Endpoint:      cfg.endpoint,
		Token:         cfg.token,
		EncryptionKey: cfg.encryptionKey,
		Interval:      1 * time.Second,
		Logger:        cfg.logger,
	}
	go func() {
		err := poller.Run(context.Background(), func(snap *configsync.Snapshot) {
			applyStagingFromSnapshot(handler, snap, cfg.logger)
		})
		if err != nil && err != context.Canceled {
			cfg.logger.Warn("config sync loop stopped", "error", err.Error())
		}
	}()
	cfg.logger.Info("config sync poller started", "endpoint", cfg.endpoint)
}

// applyStagingFromSnapshot 把导出快照映射为 staging 覆盖渠道并热应用。
func applyStagingFromSnapshot(handler *httpapi.ChatHandler, snap *configsync.Snapshot, logger *slog.Logger) {
	const (
		kEnabled   = "llm_staging_enabled"
		kProvider  = "llm_staging_provider"
		kBaseURL   = "llm_staging_base_url"
		kAPIKeyEnc = "llm_staging_api_key"
		kModel     = "llm_staging_model"
	)
	if snap.Value(kEnabled) != "true" {
		handler.SetStaging(nil)
		logger.Info("staging override cleared (disabled)", "config_version", snap.Version)
		return
	}
	provider := snap.Value(kProvider)
	apiKey := snap.Secret(kAPIKeyEnc)
	baseURL := snap.Value(kBaseURL)
	switch {
	case provider == "":
		handler.SetStaging(nil)
		logger.Warn("staging override skipped: provider empty", "config_version", snap.Version)
		return
	case apiKey == "":
		handler.SetStaging(nil)
		logger.Warn("staging override skipped: api key missing or undecryptable", "config_version", snap.Version)
		return
	case baseURL == "":
		baseURL = "https://api.openai.com/v1"
	}
	handler.SetStaging(&httpapi.StagingChannel{
		Provider: provider,
		BaseURL:  baseURL,
		APIKey:   apiKey,
		Model:    snap.Value(kModel),
	})
	logger.Info("staging override applied from config center",
		"config_version", snap.Version, "provider", provider, "base_url", baseURL)
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

// knownPlaceholderKeyPeppers are documented fill-me values. They must never be
// accepted as a live KEY_PEPPER (it salts stored API-key hashes).
var knownPlaceholderKeyPeppers = map[string]struct{}{
	"change-this-key-pepper": {},
	"change-this-pepper":     {},
	"workama-local-key-pepper-change-before-production": {},
}

// knownPlaceholderEncryptionKeys are documented fill-me / weak defaults. The
// all-0x42 base64 value is the historical dev default and must never protect
// production data.
var knownPlaceholderEncryptionKeys = map[string]struct{}{
	"QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI=": {},
}

// resolveKeyPepper reads KEY_PEPPER from the environment. There is no
// source-code fallback: an unset or empty pepper rejects startup. Documented
// placeholders (including the local/dev default) are rejected only in
// production, so development environments keep working with the compose
// fallback value.
func resolveKeyPepper() (string, error) {
	value := strings.TrimSpace(os.Getenv("KEY_PEPPER"))
	if value == "" {
		return "", errors.New("KEY_PEPPER is required and must be injected via environment")
	}
	if isProductionEnv(os.Getenv("WORKAMA_ENV")) {
		if _, ok := knownPlaceholderKeyPeppers[value]; ok {
			return "", errors.New("KEY_PEPPER is a documented placeholder; set a unique pepper in production")
		}
	}
	return value, nil
}

// resolveEncryptionKey reads ENCRYPTION_KEY from the environment. In production
// it is mandatory and must be a valid Fernet master key (32 url-safe base64
// bytes), never the documented weak default. In non-production an empty value
// (Fernet disabled) or the documented weak default (dev tolerance) is allowed,
// preserving the previous development behaviour.
func resolveEncryptionKey() (string, error) {
	value := strings.TrimSpace(os.Getenv("ENCRYPTION_KEY"))
	if value == "" {
		if isProductionEnv(os.Getenv("WORKAMA_ENV")) {
			return "", errors.New("ENCRYPTION_KEY is required in production (32 url-safe base64 bytes)")
		}
		return "", nil
	}
	if isProductionEnv(os.Getenv("WORKAMA_ENV")) {
		if _, ok := knownPlaceholderEncryptionKeys[value]; ok {
			return "", errors.New("ENCRYPTION_KEY is the documented weak default; set a unique key in production")
		}
		if !isValidFernetKey(value) {
			return "", errors.New("ENCRYPTION_KEY must be a valid Fernet key (32 url-safe base64 bytes) in production")
		}
	}
	return value, nil
}

// isValidFernetKey reports whether value decodes to exactly 32 bytes under
// either standard or url-safe base64 (Fernet master keys are 32 bytes).
func isValidFernetKey(value string) bool {
	for _, enc := range []*base64.Encoding{base64.StdEncoding, base64.URLEncoding} {
		if b, err := enc.DecodeString(value); err == nil && len(b) == 32 {
			return true
		}
	}
	return false
}

