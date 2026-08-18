// Package token defines the gateway token domain.
//
// Token 是从 gw_token 表读取的令牌元数据，用于认证（步骤①）、授权（步骤②）
// 与路由（步骤⑦：pinned_channel_id）。
package token

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"errors"
	"time"
)

// Token 是 gw_token 表的领域模型。
type Token struct {
	ID              string
	WorkspaceID     string
	Name            string
	GroupID         string
	KeyHash         string
	KeyPepper       string
	ModelWhitelist  []string
	PinnedChannelID string
	RPMLimit        int
	TPMLimit        int
	ExpiresAt       *time.Time
	RevokedAt       *time.Time
	CreatedAt       time.Time
	UpdatedAt       time.Time
}

// Verifier validates a raw API key against gw_token.
type Verifier interface {
	Verify(ctx context.Context, apiKey string) (*Token, error)
}

// ErrInvalidToken is returned when the API key cannot be matched.
var ErrInvalidToken = errors.New("invalid or revoked API key")

// ErrExpiredToken is returned when the token has expired.
var ErrExpiredToken = errors.New("api key expired")

// IsActive returns true if the token is unexpired and unrevoked.
func (t *Token) IsActive(now time.Time) bool {
	if t == nil {
		return false
	}
	if t.RevokedAt != nil && !t.RevokedAt.IsZero() {
		return false
	}
	if t.ExpiresAt != nil && !t.ExpiresAt.IsZero() && now.After(*t.ExpiresAt) {
		return false
	}
	return true
}

// CanAccessModel returns true if the token's whitelist permits the model.
// 空 whitelist 视为允许全部（与 Python 行为一致）。
func (t *Token) CanAccessModel(model string) bool {
	if t == nil {
		return false
	}
	if len(t.ModelWhitelist) == 0 {
		return true
	}
	for _, m := range t.ModelWhitelist {
		if m == model {
			return true
		}
	}
	return false
}

// HashKey computes the HMAC-SHA256 hash of an API key using the configured
// pepper. The hash matches the format stored in gw_token.key_hash.
// 与 Python hash_secret() 行为一致：HMAC-SHA256(key, pepper) → hex。
func HashKey(apiKey, pepper string) string {
	if apiKey == "" {
		return ""
	}
	mac := hmac.New(sha256.New, []byte(pepper))
	_, _ = mac.Write([]byte(apiKey))
	return hex.EncodeToString(mac.Sum(nil))
}

// EqualSecret compares two secrets in constant time.
// Empty values never match — this prevents an unset INTERNAL_TOKEN from
// authenticating requests that also omit X-Internal-Token.
func EqualSecret(provided, expected string) bool {
	if provided == "" || expected == "" {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(provided), []byte(expected)) == 1
}
