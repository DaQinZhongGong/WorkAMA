// Package configsync 从 platform-api 配置中心（/internal/config/export）
// 按版本轮询生效配置，解密密钥后回调给订阅方，实现
// 「可视化配置 → 网关热下发」：控制台改配置 → DB(UI) 发布 → Redis 版本号
// 递增 → 本轮询器拉取新快照 → 回调（如 ChatHandler.SetStaging）。
//
// 安全模型：
//   - 导出视图中密钥字段只有 Fernet 密文（platform-api 侧保证）；
//   - 本包用与 platform-api 相同的 ENCRYPTION_KEY 解密；解密失败的字段
//     跳过并记录告警，绝不 panic、不阻断轮询循环；
//   - version 未变化的响应不触发回调，避免无意义的热更新抖动。
package configsync

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"time"

	"github.com/workama/workama/apps/gateway/internal/store/pg"
)

// Snapshot 是一次导出视图的解析结果。Values 为非密钥字段明文；
// Secrets 为已解密的密钥字段明文（仅当 EncryptionKey 配置且解密成功时存在）。
type Snapshot struct {
	Version int64
	Values  map[string]string
	Secrets map[string]string
}

// Value 返回非密钥字段的字符串值（bool/int 等统一转字符串）。
func (s *Snapshot) Value(key string) string {
	if s == nil || s.Values == nil {
		return ""
	}
	return s.Values[key]
}

// Secret 返回解密后的密钥字段值。
func (s *Snapshot) Secret(key string) string {
	if s == nil || s.Secrets == nil {
		return ""
	}
	return s.Secrets[key]
}

type rawExport struct {
	Version any            `json:"version"`
	Values  map[string]any `json:"values"`
	Secrets map[string]any `json:"secrets"`
}

// Poller 周期性拉取配置中心导出视图。
type Poller struct {
	// Endpoint 形如 http://platform-api:8000/internal/config/export。
	Endpoint string
	// Token 为内部服务令牌（X-Internal-Token），由启动期校验注入。
	Token string
	// EncryptionKey 为 Fernet 主密钥（url-safe base64, 32 字节）。为空时
	// secrets 不解密（Snapshot.Secrets 为空），仅 values 可用。
	EncryptionKey string
	// Interval 轮询间隔；≤0 时取默认 2s。
	Interval time.Duration
	Client   *http.Client
	Logger   *slog.Logger
}

func (p *Poller) log() *slog.Logger {
	if p.Logger != nil {
		return p.Logger
	}
	return slog.Default()
}

func (p *Poller) interval() time.Duration {
	if p.Interval > 0 {
		return p.Interval
	}
	return 2 * time.Second
}

func (p *Poller) client() *http.Client {
	if p.Client != nil {
		return p.Client
	}
	return &http.Client{Timeout: 5 * time.Second}
}

// fetchOnce 拉取并解析一次导出视图；密钥字段就地解密进 Snapshot.Secrets。
func (p *Poller) fetchOnce(ctx context.Context) (*Snapshot, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, p.Endpoint, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-Internal-Token", p.Token)
	resp, err := p.client().Do(req)
	if err != nil {
		return nil, err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("config export http %d", resp.StatusCode)
	}
	var raw rawExport
	if err := json.NewDecoder(resp.Body).Decode(&raw); err != nil {
		return nil, fmt.Errorf("decode config export: %w", err)
	}
	snap := &Snapshot{
		Version: normalizeInt(raw.Version),
		Values:  make(map[string]string, len(raw.Values)),
		Secrets: make(map[string]string, len(raw.Secrets)),
	}
	for k, v := range raw.Values {
		snap.Values[k] = normalizeString(v)
	}
	for k, v := range raw.Secrets {
		tokenStr, ok := v.(string)
		if !ok || tokenStr == "" {
			continue
		}
		if p.EncryptionKey == "" {
			p.log().Warn("config secret skipped: ENCRYPTION_KEY not configured", "key", k)
			continue
		}
		plain, derr := pg.DecryptFernetToken(p.EncryptionKey, tokenStr, 0)
		if derr != nil {
			p.log().Warn("config secret decrypt failed; skipping key", "key", k, "error", derr.Error())
			continue
		}
		snap.Secrets[k] = string(plain)
	}
	return snap, nil
}

// Run 阻塞式轮询直到 ctx 取消。version 变化（含首次成功）触发 onChange；
// 失败按指数退避重试（interval→8×interval 封顶），恢复后立即回归常规节奏。
func (p *Poller) Run(ctx context.Context, onChange func(*Snapshot)) error {
	interval := p.interval()
	backoff := interval
	lastVersion := int64(-1)
	timer := time.NewTimer(0) // 启动即首次拉取
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-timer.C:
		}
		snap, err := p.fetchOnce(ctx)
		if err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			p.log().Warn("config sync fetch failed; backing off", "endpoint", p.Endpoint, "error", err.Error(), "retry_in", backoff.String())
			timer.Reset(backoff)
			if backoff < 8*interval {
				backoff *= 2
			}
			continue
		}
		backoff = interval
		if snap.Version != lastVersion && onChange != nil {
			onChange(snap)
		}
		lastVersion = snap.Version
		timer.Reset(interval)
	}
}

func normalizeString(v any) string {
	switch t := v.(type) {
	case nil:
		return ""
	case string:
		return t
	case bool:
		return strconv.FormatBool(t)
	case float64:
		if t == float64(int64(t)) {
			return strconv.FormatInt(int64(t), 10)
		}
		return strconv.FormatFloat(t, 'f', -1, 64)
	default:
		return fmt.Sprint(t)
	}
}

func normalizeInt(v any) int64 {
	switch t := v.(type) {
	case float64:
		return int64(t)
	case string:
		n, _ := strconv.ParseInt(t, 10, 64)
		return n
	default:
		return 0
	}
}
