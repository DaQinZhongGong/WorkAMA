// Package pg implements the gateway PostgreSQL repository using pgx v5.
//
// 该仓储覆盖 gw_token、gw_channel 与 gw_request_log 三张表的读写，供
// httpapi.ChatHandler 与 middleware 使用。
package pg

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/workama/workama/apps/gateway/internal/channel"
	"github.com/workama/workama/apps/gateway/internal/token"
)

// Gateway is the PostgreSQL-backed gateway repository.
type Gateway struct {
	pool          *pgxpool.Pool
	pepper        string
	fernetCipher  *fernetCipher
	fernetEnabled bool
}

// New constructs a Gateway repository. databaseURL must be a libpq URL.
// encryptionKey is an optional Fernet master key (32 url-safe base64 bytes) used
// to decrypt gw_channel.credential_enc so the gateway can sign upstream calls.
func New(ctx context.Context, databaseURL, pepper, encryptionKey string) (*Gateway, error) {
	if databaseURL == "" {
		return nil, fmt.Errorf("DATABASE_URL is empty")
	}
	cfg, err := pgxpool.ParseConfig(databaseURL)
	if err != nil {
		return nil, fmt.Errorf("parse database url: %w", err)
	}
	cfg.MaxConns = 32
	cfg.MinConns = 2
	cfg.MaxConnIdleTime = 5 * time.Minute
	cfg.MaxConnLifetime = 30 * time.Minute
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("open pgx pool: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping postgres: %w", err)
	}
	g := &Gateway{pool: pool, pepper: pepper}
	if encryptionKey != "" {
		if cipher, err := newFernetCipher(encryptionKey); err == nil {
			g.fernetCipher = cipher
			g.fernetEnabled = true
		}
	}
	return g, nil
}

// Close releases the underlying pool.
func (g *Gateway) Close() {
	if g == nil || g.pool == nil {
		return
	}
	g.pool.Close()
}

// CheckHealth implements server.HealthChecker by pinging the pool.
func (g *Gateway) CheckHealth(ctx context.Context) error {
	if g == nil || g.pool == nil {
		return fmt.Errorf("repository not initialized")
	}
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	if err := g.pool.Ping(ctx); err != nil {
		return fmt.Errorf("pg ping failed: %w", err)
	}
	// Also check if pool has available connections (not exhausted)
	stats := g.pool.Stat()
	if stats != nil && stats.AcquiredConns() >= stats.MaxConns() {
		return fmt.Errorf("pg connection pool exhausted (%d/%d)", stats.AcquiredConns(), stats.MaxConns())
	}
	return nil
}

// Pool returns the underlying pgxpool (for advanced users / tests).
func (g *Gateway) Pool() *pgxpool.Pool { return g.pool }

// Pepper returns the configured key pepper.
func (g *Gateway) Pepper() string { return g.pepper }

// decryptCredential 解密 gw_channel.credential_enc 中的 API Key。
// credential_enc 是 platform-api 用 Fernet 加密后存入的 base64 字节。
// 如果 Fernet cipher 未启用（未传入 encryptionKey）或解密失败，返回空字符串以避免
// 暴露原始错误（上游会返 401/403 由调用方重试或 fall-back）。
func (g *Gateway) decryptCredential(credentialEnc []byte) string {
	if len(credentialEnc) == 0 {
		return ""
	}
	if g == nil || !g.fernetEnabled || g.fernetCipher == nil {
		return ""
	}
	plain, err := g.fernetCipher.Decrypt(string(credentialEnc), 0)
	if err != nil || len(plain) == 0 {
		return ""
	}
	return string(plain)
}

// ErrNotFound is returned when a single-row query returns no rows.
var ErrNotFound = errors.New("row not found")

// GetTokenByKeyHash loads a token by its HMAC key_hash.
// 与 gw_token 表 schema 对齐：key_hash、workspace_id、model_whitelist、
// pinned_channel_id、group_id、rpm_limit、tpm_limit、expires_at、revoked_at。
func (g *Gateway) GetTokenByKeyHash(ctx context.Context, keyHash string) (*token.Token, error) {
	if g == nil || g.pool == nil {
		return nil, fmt.Errorf("repository not initialized")
	}
	row := g.pool.QueryRow(ctx, `
		SELECT id, workspace_id, COALESCE(name, ''), COALESCE(group_id, ''),
		       COALESCE(model_whitelist, ARRAY[]::text[]),
		       COALESCE(pinned_channel_id, ''),
		       COALESCE(rpm_limit, 0), COALESCE(tpm_limit, 0),
		       expires_at, revoked_at, created_at, COALESCE(updated_at, created_at)
		FROM gw_token
		WHERE key_hash = $1
		LIMIT 1
	`, keyHash)
	var t token.Token
	var expiresAt, revokedAt *time.Time
	if err := row.Scan(
		&t.ID, &t.WorkspaceID, &t.Name, &t.GroupID,
		&t.ModelWhitelist, &t.PinnedChannelID,
		&t.RPMLimit, &t.TPMLimit,
		&expiresAt, &revokedAt, &t.CreatedAt, &t.UpdatedAt,
	); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, ErrNotFound
		}
		// 容错：列名差异时退化为最小 schema 查询
		return g.fallbackGetToken(ctx, keyHash)
	}
	t.ExpiresAt = expiresAt
	t.RevokedAt = revokedAt
	return &t, nil
}

// fallbackGetToken queries a minimal set of columns in case the schema is
// an older version (no rpm_limit/tpm_limit columns). Keeps the gateway
// resilient during schema migrations.
func (g *Gateway) fallbackGetToken(ctx context.Context, keyHash string) (*token.Token, error) {
	row := g.pool.QueryRow(ctx, `
		SELECT id, workspace_id,
		       COALESCE(model_whitelist, ARRAY[]::text[]),
		       COALESCE(pinned_channel_id, '')
		FROM gw_token
		WHERE key_hash = $1
		LIMIT 1
	`, keyHash)
	var t token.Token
	if err := row.Scan(&t.ID, &t.WorkspaceID, &t.ModelWhitelist, &t.PinnedChannelID); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, fmt.Errorf("query gw_token: %w", err)
	}
	return &t, nil
}

// ListChannelsByModel loads enabled channels serving a model in a workspace.
// 查询 gw_channel WHERE workspace_id AND status='enabled' AND $model = ANY(models)。
func (g *Gateway) ListChannelsByModel(ctx context.Context, workspaceID, model string) ([]channel.Channel, error) {
	if g == nil || g.pool == nil {
		return nil, fmt.Errorf("repository not initialized")
	}
	rows, err := g.pool.Query(ctx, `
		SELECT id, workspace_id, COALESCE(name, ''), provider,
		       base_url, credential_enc,
		       COALESCE(weight, 1), COALESCE(models, ARRAY[]::text[]),
		       status,
		       created_at, COALESCE(updated_at, created_at)
		FROM gw_channel
		WHERE workspace_id = $1
		  AND status = 'enabled'
		  AND ($2 = ANY(models) OR array_length(models, 1) IS NULL)
		ORDER BY COALESCE(weight, 1) DESC, id
	`, workspaceID, model)
	if err != nil {
		return nil, fmt.Errorf("query gw_channel: %w", err)
	}
	defer rows.Close()
	out := []channel.Channel{}
	for rows.Next() {
		var c channel.Channel
		var credentialEnc []byte
		if err := rows.Scan(
			&c.ID, &c.WorkspaceID, &c.Name, &c.Provider,
			&c.BaseURL, &credentialEnc,
			&c.Weight, &c.Models,
			&c.Status,
			&c.CreatedAt, &c.UpdatedAt,
		); err != nil {
			return nil, fmt.Errorf("scan gw_channel row: %w", err)
		}
		// Protocol is derived from provider name (see adapter.NormalizeProvider).
		c.Protocol = c.Provider
		c.UpstreamModel = ""
		c.APIKey = g.decryptCredential(credentialEnc)
		out = append(out, c)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate gw_channel rows: %w", err)
	}
	return out, nil
}

// ListChannelsByWorkspace loads all enabled channels for a workspace.
// 用于 /v1/models 端点聚合 models 数组。
func (g *Gateway) ListChannelsByWorkspace(ctx context.Context, workspaceID string) ([]channel.Channel, error) {
	if g == nil || g.pool == nil {
		return nil, fmt.Errorf("repository not initialized")
	}
	rows, err := g.pool.Query(ctx, `
		SELECT id, workspace_id, COALESCE(name, ''), provider,
		       base_url, credential_enc,
		       COALESCE(weight, 1), COALESCE(models, ARRAY[]::text[]),
		       status,
		       created_at, COALESCE(updated_at, created_at)
		FROM gw_channel
		WHERE workspace_id = $1 AND status = 'enabled'
		ORDER BY id
	`, workspaceID)
	if err != nil {
		return nil, fmt.Errorf("query gw_channel by workspace: %w", err)
	}
	defer rows.Close()
	out := []channel.Channel{}
	for rows.Next() {
		var c channel.Channel
		var credentialEnc []byte
		if err := rows.Scan(
			&c.ID, &c.WorkspaceID, &c.Name, &c.Provider,
			&c.BaseURL, &credentialEnc,
			&c.Weight, &c.Models,
			&c.Status,
			&c.CreatedAt, &c.UpdatedAt,
		); err != nil {
			return nil, fmt.Errorf("scan gw_channel row: %w", err)
		}
		// Protocol is derived from provider name (see adapter.NormalizeProvider).
		c.Protocol = c.Provider
		c.UpstreamModel = ""
		c.APIKey = g.decryptCredential(credentialEnc)
		out = append(out, c)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate gw_channel rows: %w", err)
	}
	return out, nil
}

// RequestLog is the payload written to gw_request_log.
type RequestLog struct {
	RequestID        string
	WorkspaceID     string
	TokenID         *string
	ChannelID       string
	Model           string
	PromptTokens    int
	CompletionTokens int
	TotalTokens     int
	CostCredits     float64
	LatencyMS       int64
	StatusCode      int
	ErrorCode       *string
	CreatedAt       time.Time
}

// InsertRequestLog writes a metering row to gw_request_log.
// 与 Python _log_usage() 行为一致：ON CONFLICT DO NOTHING 保证幂等。
func (g *Gateway) InsertRequestLog(ctx context.Context, log *RequestLog) error {
	if g == nil || g.pool == nil {
		return fmt.Errorf("repository not initialized")
	}
	if log == nil {
		return fmt.Errorf("request log is nil")
	}
	if log.CreatedAt.IsZero() {
		log.CreatedAt = time.Now().UTC()
	}
	if log.TotalTokens == 0 && (log.PromptTokens > 0 || log.CompletionTokens > 0) {
		log.TotalTokens = log.PromptTokens + log.CompletionTokens
	}
	_, err := g.pool.Exec(ctx, `
		INSERT INTO gw_request_log(
		    request_id, workspace_id, token_id, channel_id, model,
		    prompt_tokens, completion_tokens, total_tokens, cost_credits,
		    latency_ms, status_code, error_code, created_at
		) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
		ON CONFLICT (request_id) DO NOTHING
	`,
		log.RequestID, log.WorkspaceID, log.TokenID, log.ChannelID, log.Model,
		log.PromptTokens, log.CompletionTokens, log.TotalTokens, log.CostCredits,
		log.LatencyMS, log.StatusCode, log.ErrorCode, log.CreatedAt,
	)
	if err != nil {
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) {
			return fmt.Errorf("insert gw_request_log (pgerror %s): %w", pgErr.Code, err)
		}
		return fmt.Errorf("insert gw_request_log: %w", err)
	}
	return nil
}

// Compile-time assertions that Gateway implements the domain interfaces.
var _ token.Verifier = (*Gateway)(nil)

// Verify implements token.Verifier. It accepts a raw API key, hashes it with
// the configured pepper and looks up the token by key_hash.
func (g *Gateway) Verify(ctx context.Context, apiKey string) (*token.Token, error) {
	if apiKey == "" {
		return nil, token.ErrInvalidToken
	}
	keyHash := token.HashKey(apiKey, g.pepper)
	t, err := g.GetTokenByKeyHash(ctx, keyHash)
	if err != nil {
		if errors.Is(err, ErrNotFound) {
			return nil, token.ErrInvalidToken
		}
		return nil, err
	}
	if !t.IsActive(time.Now()) {
		if t.ExpiresAt != nil && !t.ExpiresAt.IsZero() {
			return nil, token.ErrExpiredToken
		}
		return nil, token.ErrInvalidToken
	}
	return t, nil
}

// Compile-time assertion that Gateway implements channel.Repository.
var _ channel.Repository = (*Gateway)(nil)

// GetByID loads a channel by ID (implements channel.Repository).
func (g *Gateway) GetByID(ctx context.Context, id string) (*channel.Channel, error) {
	if g == nil || g.pool == nil {
		return nil, fmt.Errorf("repository not initialized")
	}
	row := g.pool.QueryRow(ctx, `
		SELECT id, workspace_id, COALESCE(name, ''), provider,
		       base_url, credential_enc,
		       COALESCE(weight, 1), COALESCE(models, ARRAY[]::text[]),
		       status,
		       created_at, COALESCE(updated_at, created_at)
		FROM gw_channel
		WHERE id = $1
	`, id)
	var c channel.Channel
	var credentialEnc []byte
	if err := row.Scan(
		&c.ID, &c.WorkspaceID, &c.Name, &c.Provider,
		&c.BaseURL, &credentialEnc,
		&c.Weight, &c.Models,
		&c.Status,
		&c.CreatedAt, &c.UpdatedAt,
	); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, fmt.Errorf("query gw_channel by id: %w", err)
	}
	c.Protocol = c.Provider
	c.UpstreamModel = ""
	c.APIKey = g.decryptCredential(credentialEnc)
	return &c, nil
}

// ListByModel implements channel.Repository.ListByModel.
func (g *Gateway) ListByModel(ctx context.Context, workspaceID, model string) ([]channel.Channel, error) {
	return g.ListChannelsByModel(ctx, workspaceID, model)
}
