package channel_test

import (
	"context"
	"errors"
	"reflect"
	"sync"
	"testing"
	"time"

	"github.com/workama/workama/apps/gateway/internal/channel"
	"github.com/workama/workama/apps/gateway/internal/relay/adapter"
)

// fakeRepository 是 Repository 接口的内存实现，用于验证接口契约与并发读取。
type fakeRepository struct {
	mu      sync.RWMutex
	byID    map[string]*channel.Channel
	byModel map[string][]channel.Channel
	getErr  error
	listErr error
}

func newFakeRepository() *fakeRepository {
	return &fakeRepository{
		byID:    map[string]*channel.Channel{},
		byModel: map[string][]channel.Channel{},
	}
}

func (r *fakeRepository) seed(chs ...channel.Channel) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, c := range chs {
		cc := c
		r.byID[c.ID] = &cc
		for _, m := range c.Models {
			r.byModel[m] = append(r.byModel[m], cc)
		}
		if len(c.Models) == 0 {
			r.byModel["*"] = append(r.byModel["*"], cc)
		}
	}
}

func (r *fakeRepository) ListByModel(_ context.Context, _ string, model string) ([]channel.Channel, error) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	if r.listErr != nil {
		return nil, r.listErr
	}
	out := append([]channel.Channel(nil), r.byModel[model]...)
	out = append(out, r.byModel["*"]...)
	return out, nil
}

func (r *fakeRepository) GetByID(_ context.Context, id string) (*channel.Channel, error) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	if r.getErr != nil {
		return nil, r.getErr
	}
	c, ok := r.byID[id]
	if !ok {
		return nil, errors.New("not found")
	}
	cp := *c
	return &cp, nil
}

// TestChannel_IsEnabled 覆盖 channel 状态机的判定逻辑：
// nil、空串、enabled、disabled、drain 等状态。
func TestChannel_IsEnabled(t *testing.T) {
	cases := []struct {
		name string
		ch   *channel.Channel
		want bool
	}{
		{"nil receiver", nil, false},
		{"empty status (legacy default)", &channel.Channel{Status: ""}, true},
		{"enabled", &channel.Channel{Status: "enabled"}, true},
		{"disabled", &channel.Channel{Status: "disabled"}, false},
		{"drain", &channel.Channel{Status: "drain"}, false},
		{"unknown status", &channel.Channel{Status: "paused"}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := tc.ch.IsEnabled(); got != tc.want {
				status := "<nil>"
				if tc.ch != nil {
					status = tc.ch.Status
				}
				t.Fatalf("IsEnabled() = %v, want %v (status=%q)", got, tc.want, status)
			}
		})
	}
}

// TestChannel_HasModel 覆盖 nil 接收者、空 Models（通配）、命中、未命中。
func TestChannel_HasModel(t *testing.T) {
	ch := &channel.Channel{
		ID:     "ch-1",
		Models: []string{"gpt-4", "gpt-3.5-turbo", "deepseek-chat"},
	}
	cases := []struct {
		name  string
		ch    *channel.Channel
		model string
		want  bool
	}{
		{"nil receiver", nil, "gpt-4", false},
		{"empty models wildcard", &channel.Channel{ID: "ch-x"}, "any-model", true},
		{"exact match", ch, "gpt-4", true},
		{"match another", ch, "deepseek-chat", true},
		{"no match", ch, "claude-3", false},
		{"empty model name not matched", ch, "", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := tc.ch.HasModel(tc.model); got != tc.want {
				t.Fatalf("HasModel(%q) = %v, want %v", tc.model, got, tc.want)
			}
		})
	}
}

// TestChannel_ToAdapter 验证领域模型到 adapter.Channel 的映射，
// 覆盖 nil 接收者与全字段填充两种情况。
func TestChannel_ToAdapter(t *testing.T) {
	t.Run("nil receiver returns zero adapter", func(t *testing.T) {
		var ch *channel.Channel
		got := ch.ToAdapter()
		if got.ID != "" || got.Provider != "" || got.Protocol != "" {
			t.Fatalf("nil ToAdapter() = %+v, want zero adapter.Channel", got)
		}
	})

	t.Run("full field mapping", func(t *testing.T) {
		now := time.Now().UTC().Truncate(time.Second)
		src := channel.Channel{
			ID:            "ch-100",
			WorkspaceID:   "ws-1",
			Name:          "primary-deepseek",
			Provider:      "deepseek",
			Protocol:      "openai",
			BaseURL:       "https://api.deepseek.com/v1",
			APIKey:        "sk-secret",
			Weight:        7,
			Models:        []string{"deepseek-chat", "deepseek-coder"},
			UpstreamModel: "deepseek-chat",
			Status:        "enabled",
			Priority:      3,
			CreatedAt:     now,
			UpdatedAt:     now,
		}
		got := src.ToAdapter()
		want := adapter.Channel{
			ID:            "ch-100",
			WorkspaceID:   "ws-1",
			Provider:      "deepseek",
			Protocol:      "openai",
			BaseURL:       "https://api.deepseek.com/v1",
			APIKey:        "sk-secret",
			Weight:        7,
			Models:        []string{"deepseek-chat", "deepseek-coder"},
			UpstreamModel: "deepseek-chat",
			Status:        "enabled",
		}
		if !reflect.DeepEqual(got, want) {
			t.Fatalf("ToAdapter() mismatch:\n got = %+v\nwant = %+v", got, want)
		}
		if src.ID != "ch-100" || src.Name != "primary-deepseek" {
			t.Fatalf("source channel mutated by ToAdapter: %+v", src)
		}
	})
}

// TestChannel_StatusStateMachine 验证常见状态字符串在
// IsEnabled / HasModel 的组合下表现稳定（状态机只读不转移）。
func TestChannel_StatusStateMachine(t *testing.T) {
	statuses := []string{"", "enabled", "disabled", "drain", "paused"}
	for _, s := range statuses {
		ch := &channel.Channel{ID: "ch-" + s, Status: s, Models: []string{"m1"}}
		enabled := ch.IsEnabled()
		hasModel := ch.HasModel("m1")
		if !hasModel {
			t.Errorf("status %q: HasModel(m1) = false, want true (decoupled)", s)
		}
		switch s {
		case "", "enabled":
			if !enabled {
				t.Errorf("status %q: IsEnabled = false, want true", s)
			}
		default:
			if enabled {
				t.Errorf("status %q: IsEnabled = true, want false", s)
			}
		}
		if ch.Status != s {
			t.Errorf("status mutated: got %q, want %q", ch.Status, s)
		}
	}
}

// TestChannel_ModelsReadConcurrency 验证 Models 切片的并发读取安全。
func TestChannel_ModelsReadConcurrency(t *testing.T) {
	repo := newFakeRepository()
	repo.seed(
		channel.Channel{ID: "a", Status: "enabled", Models: []string{"m1"}, Weight: 1},
		channel.Channel{ID: "b", Status: "enabled", Models: []string{"m1", "m2"}, Weight: 2},
		channel.Channel{ID: "c", Status: "enabled", Models: nil, Weight: 3},
	)

	const goroutines = 50
	var wg sync.WaitGroup
	wg.Add(goroutines)
	errs := make(chan error, goroutines)
	for i := 0; i < goroutines; i++ {
		go func(i int) {
			defer wg.Done()
			model := "m1"
			if i%2 == 0 {
				model = "m2"
			}
			chs, err := repo.ListByModel(context.Background(), "ws-1", model)
			if err != nil {
				errs <- err
				return
			}
			if len(chs) == 0 {
				errs <- errors.New("expected at least wildcard channel")
				return
			}
			for _, c := range chs {
				_ = c.ID
				_ = c.IsEnabled()
				_ = c.HasModel(model)
			}
		}(i)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatalf("concurrent read error: %v", err)
	}
}

// TestRepository_InterfaceContract 验证 fakeRepository 实现 Repository 接口，
// 且 GetByID / ListByModel 行为符合预期。
func TestRepository_InterfaceContract(t *testing.T) {
	var _ channel.Repository = (*fakeRepository)(nil)

	repo := newFakeRepository()
	repo.seed(
		channel.Channel{
			ID:       "ch-1",
			Models:   []string{"gpt-4"},
			Status:   "enabled",
			Weight:   5,
			Provider: "openai",
		},
		channel.Channel{
			ID:       "ch-2",
			Models:   []string{"gpt-4", "claude-3"},
			Status:   "disabled",
			Weight:   1,
			Provider: "anthropic",
		},
	)

	t.Run("GetByID hit", func(t *testing.T) {
		c, err := repo.GetByID(context.Background(), "ch-1")
		if err != nil {
			t.Fatalf("GetByID: %v", err)
		}
		if c.ID != "ch-1" || c.Provider != "openai" {
			t.Fatalf("unexpected channel: %+v", c)
		}
	})

	t.Run("GetByID miss", func(t *testing.T) {
		_, err := repo.GetByID(context.Background(), "missing")
		if err == nil {
			t.Fatal("expected error for missing id, got nil")
		}
	})

	t.Run("ListByModel filters by model and includes wildcards", func(t *testing.T) {
		chs, err := repo.ListByModel(context.Background(), "ws-1", "gpt-4")
		if err != nil {
			t.Fatalf("ListByModel: %v", err)
		}
		if len(chs) != 2 {
			t.Fatalf("expected 2 channels for gpt-4, got %d: %+v", len(chs), chs)
		}
	})

	t.Run("ListByModel propagates error", func(t *testing.T) {
		repo.listErr = errors.New("db down")
		defer func() { repo.listErr = nil }()
		if _, err := repo.ListByModel(context.Background(), "ws-1", "gpt-4"); err == nil {
			t.Fatal("expected listErr to propagate, got nil")
		}
	})

	t.Run("GetByID propagates error", func(t *testing.T) {
		repo.getErr = errors.New("db down")
		defer func() { repo.getErr = nil }()
		if _, err := repo.GetByID(context.Background(), "ch-1"); err == nil {
			t.Fatal("expected getErr to propagate, got nil")
		}
	})
}

// TestChannel_EmptyAndEdgeCases 集中处理边界情况：
// 空指针、零值 Channel、Models 为 nil vs 空切片的差异。
func TestChannel_EmptyAndEdgeCases(t *testing.T) {
	t.Run("zero value channel is enabled (empty status)", func(t *testing.T) {
		var ch channel.Channel
		if !ch.IsEnabled() {
			t.Fatal("zero-value Channel.IsEnabled() = false, want true (empty status)")
		}
	})

	t.Run("nil models treated as wildcard", func(t *testing.T) {
		ch := channel.Channel{ID: "x", Models: nil}
		if !ch.HasModel("anything") {
			t.Fatal("nil Models should be wildcard, want true")
		}
	})

	t.Run("empty slice models treated as wildcard", func(t *testing.T) {
		ch := channel.Channel{ID: "x", Models: []string{}}
		if !ch.HasModel("anything") {
			t.Fatal("empty Models slice should be wildcard, want true")
		}
	})

	t.Run("ToAdapter preserves nil models as nil", func(t *testing.T) {
		ch := channel.Channel{ID: "x", Models: nil, Status: "enabled"}
		got := ch.ToAdapter()
		if got.Models != nil {
			t.Fatalf("expected nil Models in adapter, got %v", got.Models)
		}
		if got.Status != "enabled" {
			t.Fatalf("status not mapped: got %q", got.Status)
		}
	})
}
