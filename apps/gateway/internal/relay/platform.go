package relay

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	commonobservability "github.com/workama/workama/packages/go-common/observability"
)

type Channel struct {
	ID            string `json:"id"`
	Provider      string `json:"provider"`
	BaseURL       string `json:"base_url"`
	APIKey        string `json:"api_key"`
	Weight        int    `json:"weight"`
	UpstreamModel string `json:"upstream_model"`
	Pinned        bool   `json:"pinned"`
	Fallback      bool   `json:"fallback"`
}

type FallbackPlan struct {
	Model    string    `json:"model"`
	Channels []Channel `json:"channels"`
}

type Route struct {
	WorkspaceID   string         `json:"workspace_id"`
	TokenID       *string        `json:"token_id"`
	GroupID       *string        `json:"group_id"`
	RPMLimit      int            `json:"rpm_limit"`
	TPMLimit      int            `json:"tpm_limit"`
	GroupRPMLimit int            `json:"group_rpm_limit"`
	GroupTPMLimit int            `json:"group_tpm_limit"`
	Channel       Channel        `json:"channel"`
	Channels      []Channel      `json:"channels"`
	Fallbacks     []FallbackPlan `json:"fallbacks"`
}

type RateLimitResult struct {
	Allowed    bool `json:"allowed"`
	RPMUsed    int  `json:"rpm_used"`
	TPMUsed    int  `json:"tpm_used"`
	RetryAfter int  `json:"retry_after"`
}

type RateLimitScope struct {
	ActorKey string `json:"actor_key"`
	RPMLimit int    `json:"rpm_limit"`
	TPMLimit int    `json:"tpm_limit"`
}

type ReservationResult struct {
	Duplicate     bool    `json:"duplicate"`
	ReservationID string  `json:"reservation_id"`
	EstimatedCost float64 `json:"estimated_cost"`
	Status        string  `json:"status"`
}

type ModerationResult struct {
	Action  string   `json:"action"`
	Text    string   `json:"text"`
	Matches []string `json:"matches"`
}

type PromptResolution struct {
	ID       string `json:"id"`
	Name     string `json:"name"`
	Version  int    `json:"version"`
	Checksum string `json:"checksum"`
	Content  string `json:"content"`
}

type PlatformClient struct {
	BaseURL       string
	InternalToken string
	HTTP          *http.Client
}

func NewPlatformClient(baseURL, internalToken string) *PlatformClient {
	return &PlatformClient{
		BaseURL:       strings.TrimRight(baseURL, "/"),
		InternalToken: internalToken,
		HTTP:          &http.Client{Timeout: 30 * time.Second, Transport: commonobservability.Transport(nil)},
	}
}

func (c *PlatformClient) Resolve(ctx context.Context, apiKey, workspaceID, model string) (Route, error) {
	body, err := json.Marshal(map[string]string{
		"api_key": apiKey, "workspace_id": workspaceID, "model": model,
	})
	if err != nil {
		return Route{}, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/internal/gateway/resolve", bytes.NewReader(body))
	if err != nil {
		return Route{}, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Internal-Token", c.InternalToken)
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return Route{}, fmt.Errorf("platform resolve: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		payload, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return Route{}, &ResolveError{Status: resp.StatusCode, Body: string(payload)}
	}
	var route Route
	if err := json.NewDecoder(resp.Body).Decode(&route); err != nil {
		return Route{}, fmt.Errorf("decode route: %w", err)
	}
	return route, nil
}

func (c *PlatformClient) RateLimit(ctx context.Context, actorKey string, rpmLimit, tpmLimit, estimatedTokens int) (RateLimitResult, error) {
	return c.RateLimitBatch(ctx, []RateLimitScope{{
		ActorKey: actorKey, RPMLimit: rpmLimit, TPMLimit: tpmLimit,
	}}, estimatedTokens)
}

func (c *PlatformClient) Reserve(ctx context.Context, requestID, workspaceID, model string, estimatedTokens int) (ReservationResult, error) {
	body, err := json.Marshal(map[string]any{"request_id": requestID, "workspace_id": workspaceID, "model": model, "estimated_tokens": estimatedTokens})
	if err != nil { return ReservationResult{}, err }
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/internal/gateway/reserve", bytes.NewReader(body))
	if err != nil { return ReservationResult{}, err }
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Internal-Token", c.InternalToken)
	resp, err := c.HTTP.Do(req)
	if err != nil { return ReservationResult{}, fmt.Errorf("platform reserve: %w", err) }
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		payload, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return ReservationResult{}, &ResolveError{Status: resp.StatusCode, Body: string(payload)}
	}
	var result ReservationResult
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil { return ReservationResult{}, fmt.Errorf("decode reservation: %w", err) }
	return result, nil
}

func (c *PlatformClient) Release(ctx context.Context, requestID string) error {
	body, err := json.Marshal(map[string]string{"request_id": requestID})
	if err != nil { return err }
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/internal/gateway/release", bytes.NewReader(body))
	if err != nil { return err }
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Internal-Token", c.InternalToken)
	resp, err := c.HTTP.Do(req)
	if err != nil { return fmt.Errorf("platform release: %w", err) }
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		payload, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("platform release failed with %d: %s", resp.StatusCode, payload)
	}
	return nil
}

func (c *PlatformClient) Moderate(ctx context.Context, workspaceID, direction, text, requestID string) (ModerationResult, error) {
	body, err := json.Marshal(map[string]string{
		"workspace_id": workspaceID, "direction": direction, "text": text, "request_id": requestID,
	})
	if err != nil {
		return ModerationResult{}, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/internal/security/moderate", bytes.NewReader(body))
	if err != nil {
		return ModerationResult{}, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Internal-Token", c.InternalToken)
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return ModerationResult{}, fmt.Errorf("platform moderation: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		payload, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return ModerationResult{}, fmt.Errorf("platform moderation failed with %d: %s", resp.StatusCode, payload)
	}
	var result ModerationResult
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return ModerationResult{}, fmt.Errorf("decode moderation: %w", err)
	}
	return result, nil
}

func (c *PlatformClient) ResolvePrompt(ctx context.Context, workspaceID, promptID string, variables map[string]string) (PromptResolution, error) {
	body, err := json.Marshal(map[string]any{
		"workspace_id": workspaceID,
		"prompt_id":    promptID,
		"variables":    variables,
	})
	if err != nil {
		return PromptResolution{}, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/internal/gateway/prompts/resolve", bytes.NewReader(body))
	if err != nil {
		return PromptResolution{}, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Internal-Token", c.InternalToken)
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return PromptResolution{}, fmt.Errorf("platform prompt resolve: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		payload, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return PromptResolution{}, &ResolveError{Status: resp.StatusCode, Body: string(payload)}
	}
	var result PromptResolution
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return PromptResolution{}, fmt.Errorf("decode prompt resolution: %w", err)
	}
	return result, nil
}

func (c *PlatformClient) RateLimitBatch(ctx context.Context, scopes []RateLimitScope, estimatedTokens int) (RateLimitResult, error) {
	body, err := json.Marshal(map[string]any{
		"scopes":           scopes,
		"estimated_tokens": estimatedTokens,
	})
	if err != nil {
		return RateLimitResult{}, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/internal/gateway/rate-limit/batch", bytes.NewReader(body))
	if err != nil {
		return RateLimitResult{}, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Internal-Token", c.InternalToken)
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return RateLimitResult{}, fmt.Errorf("platform rate limit: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		payload, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return RateLimitResult{}, fmt.Errorf("rate limit failed with %d: %s", resp.StatusCode, payload)
	}
	var result RateLimitResult
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return RateLimitResult{}, fmt.Errorf("decode rate limit: %w", err)
	}
	return result, nil
}

type ResolveError struct {
	Status int
	Body   string
}

func (e *ResolveError) Error() string {
	return fmt.Sprintf("route resolution failed with %d: %s", e.Status, e.Body)
}
