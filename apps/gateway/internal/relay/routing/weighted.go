package routing

import (
	"context"
	"fmt"
	"math"
	"math/rand"
	"sort"

	"github.com/workama/workama/apps/gateway/internal/relay/adapter"
)

// WeightedRouter selects a channel using weighted random selection.
// 与 Python 端实现一致：按 -ln(random())/weight 排序，取第一个候选。
// 这等价于加权 Softmax 抽样。
type WeightedRouter struct {
	rand *rand.Rand
}

// NewWeightedRouter creates a weighted router with the provided RNG source.
// 当 source 为 nil 时使用一个确定性 rand.NewSource。
func NewWeightedRouter(source rand.Source) *WeightedRouter {
	if source == nil {
		source = rand.NewSource(0)
	}
	return &WeightedRouter{rand: rand.New(source)}
}

// SelectChannel implements Router. 候选权重 <=0 视为 1 处理，避免除零。
// 当 token.PinnedChannel 非空时优先匹配 pinned 渠道；候选中不存在则返回 ErrPinnedUnavailable。
func (r *WeightedRouter) SelectChannel(_ context.Context, candidates []adapter.Channel, token *Token) (*adapter.Channel, error) {
	if len(candidates) == 0 {
		return nil, ErrNoChannels
	}
	if token != nil && token.PinnedChannel != "" {
		for i := range candidates {
			if candidates[i].ID == token.PinnedChannel {
				selected := candidates[i]
				return &selected, nil
			}
		}
		return nil, ErrPinnedUnavailable
	}
	type scored struct {
		channel adapter.Channel
		score   float64
	}
	scores := make([]scored, 0, len(candidates))
	for _, ch := range candidates {
		weight := ch.Weight
		if weight <= 0 {
			weight = 1
		}
		// -ln(rand())/weight 加权随机：权重越大，越可能取最小值（被选中）。
		u := r.rand.Float64()
		if u <= 0 {
			u = 1e-12
		}
		score := -math.Log(u) / float64(weight)
		scores = append(scores, scored{channel: ch, score: score})
	}
	sort.SliceStable(scores, func(i, j int) bool { return scores[i].score < scores[j].score })
	selected := scores[0].channel
	return &selected, nil
}

// assertWeightedRouter is a compile-time check.
var _ Router = (*WeightedRouter)(nil)

// String helper for debugging logs.
func (r *WeightedRouter) String() string { return fmt.Sprintf("WeightedRouter(rand=%T)", r.rand) }
