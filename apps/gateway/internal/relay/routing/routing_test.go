// routing 包内部测试：覆盖 routing.go / weighted.go / pinned.go / breaker.go。
//
// 测试覆盖：
// - routing.go: ErrNoChannels / ErrPinnedUnavailable 错误语义、Router 接口契约
// - weighted.go: 单候选直选、空候选报错、权重 <=0 视为 1、确定性 RNG 可复现、
//   多候选分布合理性、高权重更易被选中
// - pinned.go: 空候选报错、nil/空 token 退化为首个候选、命中 pinned、
//   pinned 不在候选中报 ErrPinnedUnavailable
// - breaker.go: 默认配置、非法配置回退默认、Allow/Record/IsOpen/Reset 状态机、
//   窗口滑动淘汰过期样本、MinimumSample 阈值、FailureRate 边界、半开放探测、
//   并发安全、独立 channel 状态
package routing

import (
	"context"
	"errors"
	"math/rand"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/workama/workama/apps/gateway/internal/relay/adapter"
)

// makeChannel 构造一个测试用 Channel。
func makeChannel(id string, weight int) adapter.Channel {
	return adapter.Channel{
		ID:     id,
		Weight: weight,
	}
}

// makeChannels 批量构造测试用 Channel。
func makeChannels(ids ...string) []adapter.Channel {
	out := make([]adapter.Channel, 0, len(ids))
	for _, id := range ids {
		out = append(out, makeChannel(id, 1))
	}
	return out
}

// newDeterministicSource 返回一个固定 seed 的 rand.Source，用于确定性测试。
func newDeterministicSource(seed int64) rand.Source {
	return rand.NewSource(seed)
}

// =============================================================================
// routing.go: 错误与接口契约测试
// =============================================================================

// TestRouterInterfaceCompileTimeCheck 验证 WeightedRouter 和 PinnedRouter 都实现了 Router 接口。
// 这通过 var _ Router = (*XXXRouter)(nil) 在源文件中已做编译期检查，
// 这里在运行时再次断言两个具体类型的接口实现。
func TestRouterInterfaceCompileTimeCheck(t *testing.T) {
	var weighted Router = NewWeightedRouter(nil)
	var pinned Router = NewPinnedRouter()
	_ = weighted
	_ = pinned
}

// TestErrNoChannelsIsSentinel 验证 ErrNoChannels 是一个稳定的哨兵错误。
func TestErrNoChannelsIsSentinel(t *testing.T) {
	if !errors.Is(ErrNoChannels, ErrNoChannels) {
		t.Fatal("ErrNoChannels should be comparable via errors.Is")
	}
	if ErrNoChannels.Error() == "" {
		t.Fatal("ErrNoChannels.Error() should not be empty")
	}
}

// TestErrPinnedUnavailableIsSentinel 验证 ErrPinnedUnavailable 是一个稳定的哨兵错误。
func TestErrPinnedUnavailableIsSentinel(t *testing.T) {
	if !errors.Is(ErrPinnedUnavailable, ErrPinnedUnavailable) {
		t.Fatal("ErrPinnedUnavailable should be comparable via errors.Is")
	}
	if ErrPinnedUnavailable.Error() == "" {
		t.Fatal("ErrPinnedUnavailable.Error() should not be empty")
	}
}

// TestErrorsAreDistinct 验证两个哨兵错误互不相同。
func TestErrorsAreDistinct(t *testing.T) {
	if errors.Is(ErrNoChannels, ErrPinnedUnavailable) {
		t.Fatal("ErrNoChannels should not be ErrPinnedUnavailable")
	}
}

// TestTokenStructFields 验证 Token 结构体的关键字段。
func TestTokenStructFields(t *testing.T) {
	tok := Token{
		ID:             "tok_1",
		WorkspaceID:    "wsp_1",
		GroupID:        "grp_1",
		PinnedChannel:  "ch_pinned",
		ModelWhitelist: []string{"gpt-4", "claude-3"},
	}
	if tok.ID != "tok_1" {
		t.Fatalf("ID = %q", tok.ID)
	}
	if tok.PinnedChannel != "ch_pinned" {
		t.Fatalf("PinnedChannel = %q", tok.PinnedChannel)
	}
	if len(tok.ModelWhitelist) != 2 {
		t.Fatalf("ModelWhitelist len = %d", len(tok.ModelWhitelist))
	}
}

// =============================================================================
// weighted.go: WeightedRouter 测试
// =============================================================================

// TestWeightedRouterEmptyCandidatesReturnsErrNoChannels 验证空候选列表返回 ErrNoChannels。
func TestWeightedRouterEmptyCandidatesReturnsErrNoChannels(t *testing.T) {
	router := NewWeightedRouter(nil)
	_, err := router.SelectChannel(context.Background(), nil, nil)
	if !errors.Is(err, ErrNoChannels) {
		t.Fatalf("err = %v, want ErrNoChannels", err)
	}
}

// TestWeightedRouterSingleCandidateReturnsIt 验证只有一个候选时直接返回它。
func TestWeightedRouterSingleCandidateReturnsIt(t *testing.T) {
	router := NewWeightedRouter(nil)
	ch := makeChannel("only", 1)
	selected, err := router.SelectChannel(context.Background(), []adapter.Channel{ch}, nil)
	if err != nil {
		t.Fatalf("err = %v", err)
	}
	if selected.ID != "only" {
		t.Fatalf("selected.ID = %q, want %q", selected.ID, "only")
	}
}

// TestWeightedRouterWeightZeroTreatedAsOne 验证权重 <=0 视为 1，不除零。
func TestWeightedRouterWeightZeroTreatedAsOne(t *testing.T) {
	// 权重 0 和负权重都不应 panic
	for _, weight := range []int{0, -1, -100} {
		router := NewWeightedRouter(nil)
		ch := makeChannel("zero", weight)
		selected, err := router.SelectChannel(context.Background(), []adapter.Channel{ch}, nil)
		if err != nil {
			t.Fatalf("weight=%d err = %v", weight, err)
		}
		if selected.ID != "zero" {
			t.Fatalf("weight=%d selected.ID = %q", weight, selected.ID)
		}
	}
}

// TestWeightedRouterMultipleCandidatesWithZeroWeightDoesNotPanic 验证多个候选都为 0 权重时不 panic。
func TestWeightedRouterMultipleCandidatesWithZeroWeightDoesNotPanic(t *testing.T) {
	router := NewWeightedRouter(nil)
	candidates := []adapter.Channel{
		makeChannel("a", 0),
		makeChannel("b", -1),
		makeChannel("c", 0),
	}
	selected, err := router.SelectChannel(context.Background(), candidates, nil)
	if err != nil {
		t.Fatalf("err = %v", err)
	}
	if selected.ID != "a" && selected.ID != "b" && selected.ID != "c" {
		t.Fatalf("selected.ID = %q, want one of a/b/c", selected.ID)
	}
}

// TestWeightedRouterDeterministicWithSeededRNG 验证相同 seed 的 RNG 产生相同的选择序列。
func TestWeightedRouterDeterministicWithSeededRNG(t *testing.T) {
	candidates := []adapter.Channel{
		makeChannel("a", 1),
		makeChannel("b", 1),
		makeChannel("c", 1),
		makeChannel("d", 1),
	}

	// 第一次运行
	router1 := NewWeightedRouter(newDeterministicSource(42))
	var selections1 []string
	for i := 0; i < 10; i++ {
		ch, err := router1.SelectChannel(context.Background(), candidates, nil)
		if err != nil {
			t.Fatalf("err = %v", err)
		}
		selections1 = append(selections1, ch.ID)
	}

	// 第二次运行，相同 seed
	router2 := NewWeightedRouter(newDeterministicSource(42))
	var selections2 []string
	for i := 0; i < 10; i++ {
		ch, err := router2.SelectChannel(context.Background(), candidates, nil)
		if err != nil {
			t.Fatalf("err = %v", err)
		}
		selections2 = append(selections2, ch.ID)
	}

	// 两次运行的选择序列应完全一致
	for i := range selections1 {
		if selections1[i] != selections2[i] {
			t.Fatalf("selection %d: %q != %q", i, selections1[i], selections2[i])
		}
	}
}

// TestWeightedRouterAllCandidatesAreSelectable 验证多次调用后所有候选都至少被选中一次
// （等权重场景下，100 次调用应覆盖所有 4 个候选）。
func TestWeightedRouterAllCandidatesAreSelectable(t *testing.T) {
	candidates := []adapter.Channel{
		makeChannel("a", 1),
		makeChannel("b", 1),
		makeChannel("c", 1),
		makeChannel("d", 1),
	}
	router := NewWeightedRouter(newDeterministicSource(12345))
	seen := make(map[string]int)
	for i := 0; i < 200; i++ {
		ch, err := router.SelectChannel(context.Background(), candidates, nil)
		if err != nil {
			t.Fatalf("err = %v", err)
		}
		seen[ch.ID]++
	}
	if len(seen) != 4 {
		t.Fatalf("only %d distinct candidates selected, want 4: %v", len(seen), seen)
	}
	for id, count := range seen {
		if count == 0 {
			t.Fatalf("candidate %q was never selected", id)
		}
	}
}

// TestWeightedRouterHigherWeightSelectedMoreOften 验证高权重候选被选中的频率更高。
// 两个候选：a 权重 9，b 权重 1。1000 次选择中 a 应明显多于 b。
func TestWeightedRouterHigherWeightSelectedMoreOften(t *testing.T) {
	candidates := []adapter.Channel{
		makeChannel("heavy", 9),
		makeChannel("light", 1),
	}
	router := NewWeightedRouter(newDeterministicSource(99))
	heavyCount := 0
	total := 1000
	for i := 0; i < total; i++ {
		ch, err := router.SelectChannel(context.Background(), candidates, nil)
		if err != nil {
			t.Fatalf("err = %v", err)
		}
		if ch.ID == "heavy" {
			heavyCount++
		}
	}
	// heavy 权重 9:1，期望约 90%。容忍 70%-95% 的统计波动。
	if heavyCount < 700 || heavyCount > 950 {
		t.Fatalf("heavyCount = %d/%d, expected between 700 and 950", heavyCount, total)
	}
}

// TestWeightedRouterReturnsPointerToCandidate 验证返回的是候选的副本（按值）。
func TestWeightedRouterReturnsPointerToCandidate(t *testing.T) {
	candidates := []adapter.Channel{
		{ID: "ch1", Provider: "openai", Weight: 1},
		{ID: "ch2", Provider: "anthropic", Weight: 1},
	}
	router := NewWeightedRouter(nil)
	selected, err := router.SelectChannel(context.Background(), candidates, nil)
	if err != nil {
		t.Fatalf("err = %v", err)
	}
	// 返回的指针应指向 candidates 中某个元素（按值复制）
	found := false
	for _, c := range candidates {
		if c.ID == selected.ID && c.Provider == selected.Provider {
			found = true
			break
		}
	}
	if !found {
		t.Fatalf("selected channel %v not in candidates", selected)
	}
}

// TestWeightedRouterNilContextDoesNotPanic 验证 nil context 不会 panic（context 仅作为参数透传）。
func TestWeightedRouterNilContextDoesNotPanic(t *testing.T) {
	router := NewWeightedRouter(nil)
	candidates := []adapter.Channel{makeChannel("a", 1)}
	//nolint:staticcheck // 测试目的：验证 nil context 不导致 panic
	ch, err := router.SelectChannel(nil, candidates, nil)
	if err != nil {
		t.Fatalf("err = %v", err)
	}
	if ch == nil {
		t.Fatal("selected channel is nil")
	}
}

// TestWeightedRouterStringHelperReturnsNonEmpty 验证 String() 辅助方法返回非空字符串。
func TestWeightedRouterStringHelperReturnsNonEmpty(t *testing.T) {
	router := NewWeightedRouter(nil)
	s := router.String()
	if s == "" {
		t.Fatal("String() should not be empty")
	}
	if s != "WeightedRouter(rand=*rand.Rand)" && s == "" {
		t.Fatalf("String() = %q", s)
	}
}

// =============================================================================
// pinned.go: PinnedRouter 测试
// =============================================================================

// TestPinnedRouterEmptyCandidatesReturnsErrNoChannels 验证空候选列表返回 ErrNoChannels。
func TestPinnedRouterEmptyCandidatesReturnsErrNoChannels(t *testing.T) {
	router := NewPinnedRouter()
	_, err := router.SelectChannel(context.Background(), nil, nil)
	if !errors.Is(err, ErrNoChannels) {
		t.Fatalf("err = %v, want ErrNoChannels", err)
	}
}

// TestPinnedRouterNilTokenReturnsFirstCandidate 验证 token 为 nil 时退化为返回首个候选。
func TestPinnedRouterNilTokenReturnsFirstCandidate(t *testing.T) {
	router := NewPinnedRouter()
	candidates := makeChannels("first", "second", "third")
	selected, err := router.SelectChannel(context.Background(), candidates, nil)
	if err != nil {
		t.Fatalf("err = %v", err)
	}
	if selected.ID != "first" {
		t.Fatalf("selected.ID = %q, want %q", selected.ID, "first")
	}
}

// TestPinnedRouterEmptyPinnedChannelReturnsFirstCandidate 验证 PinnedChannel 为空时退化为返回首个候选。
func TestPinnedRouterEmptyPinnedChannelReturnsFirstCandidate(t *testing.T) {
	router := NewPinnedRouter()
	candidates := makeChannels("first", "second")
	tok := &Token{PinnedChannel: ""}
	selected, err := router.SelectChannel(context.Background(), candidates, tok)
	if err != nil {
		t.Fatalf("err = %v", err)
	}
	if selected.ID != "first" {
		t.Fatalf("selected.ID = %q, want %q", selected.ID, "first")
	}
}

// TestPinnedRouterPinnedChannelFoundReturnsIt 验证 PinnedChannel 命中候选时返回该候选。
func TestPinnedRouterPinnedChannelFoundReturnsIt(t *testing.T) {
	router := NewPinnedRouter()
	candidates := makeChannels("first", "pinned", "third")
	tok := &Token{PinnedChannel: "pinned"}
	selected, err := router.SelectChannel(context.Background(), candidates, tok)
	if err != nil {
		t.Fatalf("err = %v", err)
	}
	if selected.ID != "pinned" {
		t.Fatalf("selected.ID = %q, want %q", selected.ID, "pinned")
	}
}

// TestPinnedRouterPinnedChannelNotInCandidatesReturnsErrPinnedUnavailable
// 验证 PinnedChannel 不在候选列表中时返回 ErrPinnedUnavailable。
func TestPinnedRouterPinnedChannelNotInCandidatesReturnsErrPinnedUnavailable(t *testing.T) {
	router := NewPinnedRouter()
	candidates := makeChannels("first", "second")
	tok := &Token{PinnedChannel: "nonexistent"}
	_, err := router.SelectChannel(context.Background(), candidates, tok)
	if !errors.Is(err, ErrPinnedUnavailable) {
		t.Fatalf("err = %v, want ErrPinnedUnavailable", err)
	}
}

// TestPinnedRouterSingleCandidatePinnedMatches 验证单候选且 pinned 匹配时返回该候选。
func TestPinnedRouterSingleCandidatePinnedMatches(t *testing.T) {
	router := NewPinnedRouter()
	candidates := []adapter.Channel{makeChannel("only", 1)}
	tok := &Token{PinnedChannel: "only"}
	selected, err := router.SelectChannel(context.Background(), candidates, tok)
	if err != nil {
		t.Fatalf("err = %v", err)
	}
	if selected.ID != "only" {
		t.Fatalf("selected.ID = %q, want %q", selected.ID, "only")
	}
}

// TestPinnedRouterPinnedMatchesLastCandidate 验证 pinned 命中最后一个候选时返回它。
func TestPinnedRouterPinnedMatchesLastCandidate(t *testing.T) {
	router := NewPinnedRouter()
	candidates := makeChannels("first", "middle", "last")
	tok := &Token{PinnedChannel: "last"}
	selected, err := router.SelectChannel(context.Background(), candidates, tok)
	if err != nil {
		t.Fatalf("err = %v", err)
	}
	if selected.ID != "last" {
		t.Fatalf("selected.ID = %q, want %q", selected.ID, "last")
	}
}

// TestPinnedRouterPinnedMatchesMiddleCandidate 验证 pinned 命中中间候选时返回它。
func TestPinnedRouterPinnedMatchesMiddleCandidate(t *testing.T) {
	router := NewPinnedRouter()
	candidates := makeChannels("first", "middle", "last")
	tok := &Token{PinnedChannel: "middle"}
	selected, err := router.SelectChannel(context.Background(), candidates, tok)
	if err != nil {
		t.Fatalf("err = %v", err)
	}
	if selected.ID != "middle" {
		t.Fatalf("selected.ID = %q, want %q", selected.ID, "middle")
	}
}

// TestPinnedRouterNilContextDoesNotPanic 验证 nil context 不会 panic。
func TestPinnedRouterNilContextDoesNotPanic(t *testing.T) {
	router := NewPinnedRouter()
	candidates := makeChannels("a")
	//nolint:staticcheck // 测试目的：验证 nil context 不导致 panic
	ch, err := router.SelectChannel(nil, candidates, nil)
	if err != nil {
		t.Fatalf("err = %v", err)
	}
	if ch == nil {
		t.Fatal("selected channel is nil")
	}
}

// =============================================================================
// breaker.go: CircuitBreaker 测试
// =============================================================================

// TestDefaultCircuitBreakerConfigValues 验证默认配置符合设计规格。
func TestDefaultCircuitBreakerConfigValues(t *testing.T) {
	cfg := DefaultCircuitBreakerConfig()
	if cfg.Window != 30*time.Second {
		t.Fatalf("Window = %v, want 30s", cfg.Window)
	}
	if cfg.OpenDuration != 60*time.Second {
		t.Fatalf("OpenDuration = %v, want 60s", cfg.OpenDuration)
	}
	if cfg.MinimumSample != 10 {
		t.Fatalf("MinimumSample = %d, want 10", cfg.MinimumSample)
	}
	if cfg.FailureRate != 0.5 {
		t.Fatalf("FailureRate = %v, want 0.5", cfg.FailureRate)
	}
}

// TestNewCircuitBreakerUsesDefaultConfig 验证 NewCircuitBreaker 使用默认配置。
func TestNewCircuitBreakerUsesDefaultConfig(t *testing.T) {
	b := NewCircuitBreaker()
	if b.cfg.Window != 30*time.Second {
		t.Fatalf("cfg.Window = %v", b.cfg.Window)
	}
	if b.cfg.MinimumSample != 10 {
		t.Fatalf("cfg.MinimumSample = %d", b.cfg.MinimumSample)
	}
}

// TestNewCircuitBreakerWithConfigUsesProvidedValues 验证显式配置被采用。
func TestNewCircuitBreakerWithConfigUsesProvidedValues(t *testing.T) {
	cfg := CircuitBreakerConfig{
		Window:        10 * time.Second,
		OpenDuration:  30 * time.Second,
		MinimumSample: 5,
		FailureRate:   0.3,
	}
	b := NewCircuitBreakerWithConfig(cfg)
	if b.cfg.Window != 10*time.Second {
		t.Fatalf("cfg.Window = %v, want 10s", b.cfg.Window)
	}
	if b.cfg.OpenDuration != 30*time.Second {
		t.Fatalf("cfg.OpenDuration = %v, want 30s", b.cfg.OpenDuration)
	}
	if b.cfg.MinimumSample != 5 {
		t.Fatalf("cfg.MinimumSample = %d, want 5", b.cfg.MinimumSample)
	}
	if b.cfg.FailureRate != 0.3 {
		t.Fatalf("cfg.FailureRate = %v, want 0.3", b.cfg.FailureRate)
	}
}

// TestNewCircuitBreakerWithInvalidConfigFallsBackToDefault 验证非法配置回退到默认值。
func TestNewCircuitBreakerWithInvalidConfigFallsBackToDefault(t *testing.T) {
	cases := []CircuitBreakerConfig{
		{Window: 0, OpenDuration: 60 * time.Second, MinimumSample: 10, FailureRate: 0.5},
		{Window: 30 * time.Second, OpenDuration: 0, MinimumSample: 10, FailureRate: 0.5},
		{Window: 30 * time.Second, OpenDuration: 60 * time.Second, MinimumSample: 0, FailureRate: 0.5},
		{Window: 30 * time.Second, OpenDuration: 60 * time.Second, MinimumSample: 10, FailureRate: 0},
		{Window: 30 * time.Second, OpenDuration: 60 * time.Second, MinimumSample: 10, FailureRate: -0.1},
	}
	for i, cfg := range cases {
		b := NewCircuitBreakerWithConfig(cfg)
		defaultCfg := DefaultCircuitBreakerConfig()
		if b.cfg.Window != defaultCfg.Window {
			t.Fatalf("case %d: cfg.Window = %v, want default %v", i, b.cfg.Window, defaultCfg.Window)
		}
		if b.cfg.MinimumSample != defaultCfg.MinimumSample {
			t.Fatalf("case %d: cfg.MinimumSample = %d, want default %d", i, b.cfg.MinimumSample, defaultCfg.MinimumSample)
		}
	}
}

// TestCircuitBreakerAllowReturnsTrueForNewChannel 验证新 channel 默认被允许。
func TestCircuitBreakerAllowReturnsTrueForNewChannel(t *testing.T) {
	b := NewCircuitBreaker()
	if !b.Allow("new_channel") {
		t.Fatal("Allow(new_channel) should be true")
	}
}

// TestCircuitBreakerDoesNotOpenBelowMinimumSample 验证样本数不足 MinimumSample 时不会打开熔断。
func TestCircuitBreakerDoesNotOpenBelowMinimumSample(t *testing.T) {
	b := NewCircuitBreaker()
	now := time.Unix(1000, 0)
	b.SetNow(func() time.Time { return now })
	// 默认 MinimumSample=10，全失败 9 次也不应打开
	for i := 0; i < 9; i++ {
		b.Record("ch", true)
	}
	if !b.Allow("ch") {
		t.Fatal("breaker should not open below minimum sample size")
	}
	if b.IsOpen("ch") {
		t.Fatal("IsOpen should be false below minimum sample size")
	}
}

// TestCircuitBreakerOpensAtMinimumSampleWithHighFailureRate 验证达到 MinimumSample 且失败率超阈值时打开熔断。
func TestCircuitBreakerOpensAtMinimumSampleWithHighFailureRate(t *testing.T) {
	b := NewCircuitBreaker()
	now := time.Unix(2000, 0)
	b.SetNow(func() time.Time { return now })
	// 10 个样本，6 个失败（60% > 50%）→ 打开
	for i := 0; i < 6; i++ {
		b.Record("ch", true)
	}
	for i := 0; i < 4; i++ {
		b.Record("ch", false)
	}
	if b.Allow("ch") {
		t.Fatal("breaker should be open with 60% failure rate at minimum sample")
	}
	if !b.IsOpen("ch") {
		t.Fatal("IsOpen should be true")
	}
}

// TestCircuitBreakerDoesNotOpenWhenFailureRateAtThreshold 验证失败率恰好等于阈值（50%）时不打开熔断。
// 源码使用 > 比较（float64(failures) > b.cfg.FailureRate*float64(len(state.outcomes))），
// 所以 5/10=50% 不打开，6/10=60% 打开。
func TestCircuitBreakerDoesNotOpenWhenFailureRateAtThreshold(t *testing.T) {
	b := NewCircuitBreaker()
	now := time.Unix(3000, 0)
	b.SetNow(func() time.Time { return now })
	// 10 个样本，5 个失败（50% == 50% 阈值，使用 > 不打开）
	for i := 0; i < 5; i++ {
		b.Record("ch", true)
	}
	for i := 0; i < 5; i++ {
		b.Record("ch", false)
	}
	if !b.Allow("ch") {
		t.Fatal("breaker should not open when failure rate equals threshold (50%)")
	}
}

// TestCircuitBreakerOpensWhenFailureRateJustAboveThreshold 验证失败率略高于阈值时打开熔断。
func TestCircuitBreakerOpensWhenFailureRateJustAboveThreshold(t *testing.T) {
	cfg := CircuitBreakerConfig{
		Window:        30 * time.Second,
		OpenDuration:  60 * time.Second,
		MinimumSample: 10,
		FailureRate:   0.5,
	}
	b := NewCircuitBreakerWithConfig(cfg)
	now := time.Unix(4000, 0)
	b.SetNow(func() time.Time { return now })
	// 10 个样本，6 个失败（60% > 50%）
	for i := 0; i < 6; i++ {
		b.Record("ch", true)
	}
	for i := 0; i < 4; i++ {
		b.Record("ch", false)
	}
	if b.Allow("ch") {
		t.Fatal("breaker should be open when failure rate is 60% > 50%")
	}
}

// TestCircuitBreakerRecoversAfterOpenDuration 验证熔断打开后经过 OpenDuration 时间自动恢复（半开放探测）。
func TestCircuitBreakerRecoversAfterOpenDuration(t *testing.T) {
	b := NewCircuitBreaker()
	now := time.Unix(5000, 0)
	b.SetNow(func() time.Time { return now })
	// 触发熔断
	for i := 0; i < 6; i++ {
		b.Record("ch", true)
	}
	for i := 0; i < 4; i++ {
		b.Record("ch", false)
	}
	if b.Allow("ch") {
		t.Fatal("breaker should be open")
	}
	// 推进 60 秒（默认 OpenDuration），熔断应恢复
	now = now.Add(61 * time.Second)
	if !b.Allow("ch") {
		t.Fatal("breaker should recover after OpenDuration")
	}
}

// TestCircuitBreakerStillOpenJustBeforeOpenDuration 验证熔断在 OpenDuration 之前仍然打开。
func TestCircuitBreakerStillOpenJustBeforeOpenDuration(t *testing.T) {
	b := NewCircuitBreaker()
	now := time.Unix(6000, 0)
	b.SetNow(func() time.Time { return now })
	for i := 0; i < 6; i++ {
		b.Record("ch", true)
	}
	for i := 0; i < 4; i++ {
		b.Record("ch", false)
	}
	// 推进 59 秒（< 60s OpenDuration），熔断仍应打开
	now = now.Add(59 * time.Second)
	if b.Allow("ch") {
		t.Fatal("breaker should still be open just before OpenDuration elapses")
	}
}

// TestCircuitBreakerResetsStateAfterRecovery 验证熔断恢复后状态被清空（半开放后允许重新累积样本）。
func TestCircuitBreakerResetsStateAfterRecovery(t *testing.T) {
	b := NewCircuitBreaker()
	now := time.Unix(7000, 0)
	b.SetNow(func() time.Time { return now })
	// 触发熔断
	for i := 0; i < 10; i++ {
		b.Record("ch", true)
	}
	if b.Allow("ch") {
		t.Fatal("breaker should be open")
	}
	// 推进超过 OpenDuration
	now = now.Add(61 * time.Second)
	if !b.Allow("ch") {
		t.Fatal("breaker should recover")
	}
	// 恢复后再调用 Allow 应再次返回 true（状态已清空）
	// 并且 IsOpen 应为 false
	if b.IsOpen("ch") {
		t.Fatal("IsOpen should be false after recovery")
	}
}

// TestCircuitBreakerWindowSlidesOutOldSamples 验证窗口滑动淘汰过期样本。
// 在 t0 记录 9 次失败（< MinimumSample=10，不熔断），
// 推进时间超过 Window（30s）后再记录 1 次失败，
// 旧的 9 次应被淘汰，只剩 1 次失败，不触发熔断。
func TestCircuitBreakerWindowSlidesOutOldSamples(t *testing.T) {
	b := NewCircuitBreaker()
	now := time.Unix(8000, 0)
	b.SetNow(func() time.Time { return now })
	// 9 次失败（不足 MinimumSample=10）
	for i := 0; i < 9; i++ {
		b.Record("ch", true)
	}
	if !b.Allow("ch") {
		t.Fatal("breaker should not open with only 9 samples")
	}
	// 推进 31 秒（> Window=30s），旧样本过期
	now = now.Add(31 * time.Second)
	// 1 次失败（旧 9 次已淘汰，只剩 1 次失败）
	b.Record("ch", true)
	if !b.Allow("ch") {
		t.Fatal("breaker should not open after window slid (only 1 sample)")
	}
}

// TestCircuitBreakerWindowRetainsSamplesWithinWindow 验证窗口内的样本被保留。
func TestCircuitBreakerWindowRetainsSamplesWithinWindow(t *testing.T) {
	b := NewCircuitBreaker()
	now := time.Unix(9000, 0)
	b.SetNow(func() time.Time { return now })
	// 5 次失败
	for i := 0; i < 5; i++ {
		b.Record("ch", true)
	}
	// 推进 29 秒（< Window=30s），样本应保留
	now = now.Add(29 * time.Second)
	// 再 5 次失败（共 10 个样本，全失败 → 100% > 50% → 熔断）
	for i := 0; i < 5; i++ {
		b.Record("ch", true)
	}
	if b.Allow("ch") {
		t.Fatal("breaker should be open with 10 failures within window")
	}
}

// TestCircuitBreakerResetsChannelState 验证 Reset 清除指定 channel 的状态。
func TestCircuitBreakerResetsChannelState(t *testing.T) {
	b := NewCircuitBreaker()
	now := time.Unix(10000, 0)
	b.SetNow(func() time.Time { return now })
	// 触发熔断
	for i := 0; i < 10; i++ {
		b.Record("ch", true)
	}
	if b.Allow("ch") {
		t.Fatal("breaker should be open")
	}
	// Reset 清除状态
	b.Reset("ch")
	if !b.Allow("ch") {
		t.Fatal("Allow should return true after Reset")
	}
	if b.IsOpen("ch") {
		t.Fatal("IsOpen should return false after Reset")
	}
}

// TestCircuitBreakerResetNonExistentChannelIsNoop 验证 Reset 不存在的 channel 是 no-op。
func TestCircuitBreakerResetNonExistentChannelIsNoop(t *testing.T) {
	b := NewCircuitBreaker()
	// 不应 panic
	b.Reset("never_seen")
	if !b.Allow("never_seen") {
		t.Fatal("Allow should return true for unknown channel after Reset")
	}
}

// TestCircuitBreakerTracksChannelsIndependently 验证不同 channel 的状态互相独立。
func TestCircuitBreakerTracksChannelsIndependently(t *testing.T) {
	b := NewCircuitBreaker()
	now := time.Unix(11000, 0)
	b.SetNow(func() time.Time { return now })
	// 让 channel_a 熔断
	for i := 0; i < 10; i++ {
		b.Record("channel_a", true)
	}
	// channel_b 应不受影响
	if !b.Allow("channel_b") {
		t.Fatal("channel_b should not be affected by channel_a's breaker")
	}
	if b.IsOpen("channel_b") {
		t.Fatal("IsOpen(channel_b) should be false")
	}
	// channel_a 应被熔断
	if b.Allow("channel_a") {
		t.Fatal("channel_a should be open")
	}
}

// TestCircuitBreakerAllSuccessDoesNotOpen 验证全成功不会打开熔断。
func TestCircuitBreakerAllSuccessDoesNotOpen(t *testing.T) {
	b := NewCircuitBreaker()
	now := time.Unix(12000, 0)
	b.SetNow(func() time.Time { return now })
	for i := 0; i < 100; i++ {
		b.Record("ch", false)
	}
	if !b.Allow("ch") {
		t.Fatal("breaker should not open with all successes")
	}
}

// TestCircuitBreakerConcurrentRecordIsSafe 验证并发 Record 调用是线程安全的。
func TestCircuitBreakerConcurrentRecordIsSafe(t *testing.T) {
	b := NewCircuitBreaker()
	now := time.Unix(13000, 0)
	b.SetNow(func() time.Time { return now })

	const goroutines = 100
	var wg sync.WaitGroup
	for i := 0; i < goroutines; i++ {
		wg.Add(1)
		go func(failed bool) {
			defer wg.Done()
			b.Record("concurrent_ch", failed)
		}(i%2 == 0)
	}
	wg.Wait()

	// 不应 panic，且最终 Allow 返回布尔值
	_ = b.Allow("concurrent_ch")
}

// TestCircuitBreakerConcurrentAllowIsSafe 验证并发 Allow 调用是线程安全的。
func TestCircuitBreakerConcurrentAllowIsSafe(t *testing.T) {
	b := NewCircuitBreaker()
	now := time.Unix(14000, 0)
	b.SetNow(func() time.Time { return now })

	const goroutines = 100
	var allowed int64
	var wg sync.WaitGroup
	for i := 0; i < goroutines; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if b.Allow("concurrent_allow_ch") {
				atomic.AddInt64(&allowed, 1)
			}
		}()
	}
	wg.Wait()

	// 全部应被允许（无任何 Record 调用）
	if int(allowed) != goroutines {
		t.Fatalf("allowed = %d, want %d", allowed, goroutines)
	}
}

// TestCircuitBreakerSetNowInjectsCustomClock 验证 SetNow 注入自定义时钟。
func TestCircuitBreakerSetNowInjectsCustomClock(t *testing.T) {
	b := NewCircuitBreaker()
	current := time.Unix(15000, 0)
	b.SetNow(func() time.Time { return current })

	// 触发熔断
	for i := 0; i < 10; i++ {
		b.Record("ch", true)
	}
	if b.Allow("ch") {
		t.Fatal("breaker should be open")
	}
	// 推进时钟
	current = current.Add(61 * time.Second)
	if !b.Allow("ch") {
		t.Fatal("breaker should recover after advancing injected clock")
	}
}

// TestCircuitBreakerRecordsMixedOutcomes 验证混合成功/失败记录后的熔断决策。
func TestCircuitBreakerRecordsMixedOutcomes(t *testing.T) {
	cfg := CircuitBreakerConfig{
		Window:        30 * time.Second,
		OpenDuration:  60 * time.Second,
		MinimumSample: 20,
		FailureRate:   0.25, // 25% 阈值
	}
	b := NewCircuitBreakerWithConfig(cfg)
	now := time.Unix(16000, 0)
	b.SetNow(func() time.Time { return now })
	// 20 个样本：15 成功 + 5 失败 = 25% 失败率，恰好等于阈值，不打开（> 比较）
	for i := 0; i < 15; i++ {
		b.Record("ch", false)
	}
	for i := 0; i < 5; i++ {
		b.Record("ch", true)
	}
	if !b.Allow("ch") {
		t.Fatal("breaker should not open when failure rate equals threshold (25%)")
	}

	// 再加 1 个失败 → 6/21 ≈ 28.6% > 25% → 打开
	b.Record("ch", true)
	if b.Allow("ch") {
		t.Fatal("breaker should open when failure rate exceeds threshold (28.6% > 25%)")
	}
}

// TestCircuitBreakerOpenDurationBoundary 验证恰好经过 OpenDuration 时熔断恢复的边界。
func TestCircuitBreakerOpenDurationBoundary(t *testing.T) {
	cfg := CircuitBreakerConfig{
		Window:        30 * time.Second,
		OpenDuration:  60 * time.Second,
		MinimumSample: 5,
		FailureRate:   0.5,
	}
	b := NewCircuitBreakerWithConfig(cfg)
	now := time.Unix(17000, 0)
	b.SetNow(func() time.Time { return now })
	// 触发熔断
	for i := 0; i < 3; i++ {
		b.Record("ch", true)
	}
	for i := 0; i < 2; i++ {
		b.Record("ch", false)
	}
	if b.Allow("ch") {
		t.Fatal("breaker should be open")
	}
	// 推进恰好 60 秒（== OpenDuration），Allow 检查 openUntil.After(now)
	// openUntil = trigger_time + OpenDuration，now = trigger_time + 60s
	// openUntil.After(now) 为 false（相等不算 After）→ 恢复
	now = now.Add(60 * time.Second)
	if !b.Allow("ch") {
		t.Fatal("breaker should recover at exactly OpenDuration boundary")
	}
}

// TestCircuitBreakerRepeatedRecordAfterRecovery 验证恢复后再次累积样本可以再次触发熔断。
func TestCircuitBreakerRepeatedRecordAfterRecovery(t *testing.T) {
	b := NewCircuitBreaker()
	now := time.Unix(18000, 0)
	b.SetNow(func() time.Time { return now })

	// 第一次触发熔断
	for i := 0; i < 6; i++ {
		b.Record("ch", true)
	}
	for i := 0; i < 4; i++ {
		b.Record("ch", false)
	}
	if b.Allow("ch") {
		t.Fatal("breaker should be open first time")
	}

	// 恢复
	now = now.Add(61 * time.Second)
	if !b.Allow("ch") {
		t.Fatal("breaker should recover")
	}

	// 再次累积样本触发熔断
	for i := 0; i < 6; i++ {
		b.Record("ch", true)
	}
	for i := 0; i < 4; i++ {
		b.Record("ch", false)
	}
	if b.Allow("ch") {
		t.Fatal("breaker should be open second time")
	}
}
