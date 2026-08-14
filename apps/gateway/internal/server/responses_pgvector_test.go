package server

import (
	"context"
	"errors"
	"io"
	"math"
	"strings"
	"testing"
	"time"
)

// =============================================================================
// 辅助桩类型：用于 pgvector 基础实现的错误路径测试
// 以下桩类型故意与 responses_test.go 中的桩分离，专注于错误注入场景。
// =============================================================================

// errorSQLRows 在 Scan/Err 时返回预设错误，用于测试扫描失败路径。
type errorSQLRows struct {
	scanErr error
	errErr  error
	yielded bool
	closed  bool
}

func (rows *errorSQLRows) Next() bool {
	if rows.yielded {
		return false
	}
	rows.yielded = true
	return true
}

func (rows *errorSQLRows) Scan(...any) error { return rows.scanErr }

func (rows *errorSQLRows) Close() error {
	rows.closed = true
	return nil
}

func (rows *errorSQLRows) Err() error { return rows.errErr }

// emptySQLRows 永不返回数据行，用于测试无命中场景。
type emptySQLRows struct{ closed bool }

func (rows *emptySQLRows) Next() bool { return false }
func (rows *emptySQLRows) Scan(...any) error {
	return errors.New("emptySQLRows has no data to scan")
}
func (rows *emptySQLRows) Close() error {
	rows.closed = true
	return nil
}
func (rows *emptySQLRows) Err() error { return nil }

// scriptedSQLExecutor 按调用顺序返回预设的 rows/error，用于精确控制多次 QueryContext 的行为。
type scriptedSQLExecutor struct {
	queryResults []struct {
		rows ResponseSemanticCacheSQLRows
		err  error
	}
	execErr   error
	calls     int
	execArgs  []any
	execQuery string
}

func (executor *scriptedSQLExecutor) QueryContext(_ context.Context, _ string, args ...any) (ResponseSemanticCacheSQLRows, error) {
	defer func() { executor.calls++ }()
	executor.execArgs = append(executor.execArgs, args...)
	if executor.calls >= len(executor.queryResults) {
		return &emptySQLRows{}, nil
	}
	result := executor.queryResults[executor.calls]
	return result.rows, result.err
}

func (executor *scriptedSQLExecutor) ExecContext(_ context.Context, query string, args ...any) error {
	executor.execQuery = query
	executor.execArgs = append(executor.execArgs, args...)
	return executor.execErr
}

// nilCloser 是一个实现 io.Closer 但不做任何事情的闭包器，用于测试 closer fallback。
type nilCloser struct{ closed int }

func (closer *nilCloser) Close() error {
	closer.closed++
	return nil
}

// =============================================================================
// responseSemanticCacheVectorText 测试
// =============================================================================

func TestResponseSemanticCacheVectorTextValid(t *testing.T) {
	// 验证合法嵌入向量能被正确序列化为 pgvector 文本格式 [v1,v2,...]
	embedding := responseSemanticEmbedding("valid embedding text")
	text, err := responseSemanticCacheVectorText(embedding)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.HasPrefix(text, "[") || !strings.HasSuffix(text, "]") {
		t.Fatalf("vector text not bracketed: %s", text)
	}
	// 反序列化后应与原始向量一致（验证往返一致性）
	parsed, err := responseSemanticCacheParseVectorText(text)
	if err != nil {
		t.Fatalf("round-trip parse failed: %v", err)
	}
	if len(parsed) != len(embedding) {
		t.Fatalf("round-trip dimension mismatch: got %d, want %d", len(parsed), len(embedding))
	}
	for index := range embedding {
		if parsed[index] != embedding[index] {
			t.Fatalf("round-trip value mismatch at index %d: got %v, want %v", index, parsed[index], embedding[index])
		}
	}
}

func TestResponseSemanticCacheVectorTextInvalidDimensions(t *testing.T) {
	// 维度不匹配（空向量、过短、过长）都应报错
	tests := []struct {
		name      string
		embedding []float64
	}{
		{name: "empty", embedding: []float64{}},
		{name: "too short", embedding: make([]float64, responseSemanticCacheEmbeddingDimensions-1)},
		{name: "too long", embedding: make([]float64, responseSemanticCacheEmbeddingDimensions+1)},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if _, err := responseSemanticCacheVectorText(test.embedding); err == nil {
				t.Fatal("expected error for invalid dimensions, got nil")
			}
		})
	}
}

func TestResponseSemanticCacheVectorTextNonFiniteValues(t *testing.T) {
	// NaN 和 Inf 值不可序列化为 pgvector 文本
	tests := []struct {
		name  string
		value float64
	}{
		{name: "NaN", value: math.NaN()},
		{name: "positive infinity", value: math.Inf(1)},
		{name: "negative infinity", value: math.Inf(-1)},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			embedding := make([]float64, responseSemanticCacheEmbeddingDimensions)
			embedding[0] = test.value
			if _, err := responseSemanticCacheVectorText(embedding); err == nil {
				t.Fatalf("expected error for %s, got nil", test.name)
			}
		})
	}
}

// =============================================================================
// responseSemanticCacheParseVectorText 测试
// =============================================================================

func TestResponseSemanticCacheParseVectorTextValid(t *testing.T) {
	// 合法向量文本应被正确解析为 float64 切片
	original := responseSemanticEmbedding("parse me")
	serialized, err := responseSemanticCacheVectorText(original)
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := responseSemanticCacheParseVectorText(serialized)
	if err != nil {
		t.Fatalf("parse failed: %v", err)
	}
	if len(parsed) != responseSemanticCacheEmbeddingDimensions {
		t.Fatalf("parsed dimension = %d, want %d", len(parsed), responseSemanticCacheEmbeddingDimensions)
	}
	for index := range original {
		if parsed[index] != original[index] {
			t.Fatalf("parsed value at %d = %v, want %v", index, parsed[index], original[index])
		}
	}
}

func TestResponseSemanticCacheParseVectorTextInvalidSyntax(t *testing.T) {
	// 缺少方括号、空字符串等语法错误应被拒绝
	tests := []struct {
		name  string
		input string
	}{
		{name: "empty string", input: ""},
		{name: "missing opening bracket", input: "1,2,3]"},
		{name: "missing closing bracket", input: "[1,2,3"},
		{name: "no brackets", input: "1,2,3"},
		{name: "only brackets", input: "[]"},
		{name: "whitespace only", input: "   "},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if _, err := responseSemanticCacheParseVectorText(test.input); err == nil {
				t.Fatalf("expected error for %q, got nil", test.input)
			}
		})
	}
}

func TestResponseSemanticCacheParseVectorTextInvalidDimensions(t *testing.T) {
	// 维度不匹配的向量文本应被拒绝
	tests := []struct {
		name  string
		input string
	}{
		{name: "too few", input: "[1.0,2.0]"},
		{name: "too many", input: "[" + strings.Repeat("1.0,", responseSemanticCacheEmbeddingDimensions) + "1.0]"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if _, err := responseSemanticCacheParseVectorText(test.input); err == nil {
				t.Fatalf("expected dimension error for %q, got nil", test.input)
			}
		})
	}
}

func TestResponseSemanticCacheParseVectorTextInvalidValues(t *testing.T) {
	// 非数字值和无穷值应被拒绝
	dim := responseSemanticCacheEmbeddingDimensions
	tests := []struct {
		name  string
		input string
	}{
		{name: "non-numeric", input: "[" + strings.Repeat("0,", dim-1) + "not_a_number]"},
		{name: "NaN literal", input: "[" + strings.Repeat("0,", dim-1) + "NaN]"},
		{name: "+Inf literal", input: "[" + strings.Repeat("0,", dim-1) + "+Inf]"},
		{name: "-Inf literal", input: "[" + strings.Repeat("0,", dim-1) + "-Inf]"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if _, err := responseSemanticCacheParseVectorText(test.input); err == nil {
				t.Fatalf("expected value error for %q, got nil", test.input)
			}
		})
	}
}

func TestResponseSemanticCacheParseVectorTextWhitespaceTolerance(t *testing.T) {
	// 值周围的空格应被容忍
	values := make([]string, responseSemanticCacheEmbeddingDimensions)
	for index := range values {
		values[index] = " 0.5 "
	}
	input := "[" + strings.Join(values, " , ") + "]"
	parsed, err := responseSemanticCacheParseVectorText(input)
	if err != nil {
		t.Fatalf("whitespace-tolerant parse failed: %v", err)
	}
	for index, value := range parsed {
		if value != 0.5 {
			t.Fatalf("parsed value at %d = %v, want 0.5", index, value)
		}
	}
}

// =============================================================================
// newPGVectorResponseSemanticCache 构造函数错误路径测试
// =============================================================================

func TestNewPGVectorResponseSemanticCacheEmptyURL(t *testing.T) {
	// 空 URL 或纯空白 URL 应返回错误
	tests := []string{"", "   ", "\t\n"}
	for _, url := range tests {
		if _, err := newPGVectorResponseSemanticCache(url); err == nil {
			t.Fatalf("expected error for empty URL %q, got nil", url)
		}
	}
}

func TestNewPGVectorResponseSemanticCacheNilFactory(t *testing.T) {
	// 未注入 SQL 驱动工厂时应返回错误
	SetResponseSemanticCacheSQLExecutorFactory(nil)
	defer SetResponseSemanticCacheSQLExecutorFactory(nil)
	if _, err := newPGVectorResponseSemanticCache("postgres://localhost/db"); err == nil {
		t.Fatal("expected error when factory is nil, got nil")
	}
}

func TestNewPGVectorResponseSemanticCacheFactoryError(t *testing.T) {
	// 工厂返回错误时应包装并返回
	factoryErr := errors.New("factory boom")
	SetResponseSemanticCacheSQLExecutorFactory(func(_ string) (ResponseSemanticCacheSQLExecutor, io.Closer, error) {
		return nil, nil, factoryErr
	})
	defer SetResponseSemanticCacheSQLExecutorFactory(nil)
	repository, err := newPGVectorResponseSemanticCache("postgres://localhost/db")
	if err == nil || !strings.Contains(err.Error(), "open PostgreSQL connection pool") {
		t.Fatalf("expected wrapped factory error, got %v", err)
	}
	if repository != nil {
		t.Fatalf("expected nil repository on factory error, got %T", repository)
	}
}

func TestNewPGVectorResponseSemanticCacheNilExecutorWithCloser(t *testing.T) {
	// 工厂返回 nil executor 但非 nil closer 时，应关闭 closer 并报错
	closer := &nilCloser{}
	SetResponseSemanticCacheSQLExecutorFactory(func(_ string) (ResponseSemanticCacheSQLExecutor, io.Closer, error) {
		return nil, closer, nil
	})
	defer SetResponseSemanticCacheSQLExecutorFactory(nil)
	repository, err := newPGVectorResponseSemanticCache("postgres://localhost/db")
	if err == nil || !strings.Contains(err.Error(), "nil executor") {
		t.Fatalf("expected nil executor error, got %v", err)
	}
	if repository != nil {
		t.Fatal("expected nil repository")
	}
	if closer.closed != 1 {
		t.Fatalf("expected closer to be called once, got %d", closer.closed)
	}
}

func TestNewPGVectorResponseSemanticCacheNilExecutorWithoutCloser(t *testing.T) {
	// 工厂返回 nil executor 和 nil closer 时，应报错但不 panic
	SetResponseSemanticCacheSQLExecutorFactory(func(_ string) (ResponseSemanticCacheSQLExecutor, io.Closer, error) {
		return nil, nil, nil
	})
	defer SetResponseSemanticCacheSQLExecutorFactory(nil)
	repository, err := newPGVectorResponseSemanticCache("postgres://localhost/db")
	if err == nil || !strings.Contains(err.Error(), "nil executor") {
		t.Fatalf("expected nil executor error, got %v", err)
	}
	if repository != nil {
		t.Fatal("expected nil repository")
	}
}

func TestNewPGVectorResponseSemanticCacheCloserFallback(t *testing.T) {
	// 工厂返回 nil closer 但 executor 实现 io.Closer 时，应使用 executor 作为 closer
	executor := &closableExecutor{}
	SetResponseSemanticCacheSQLExecutorFactory(func(_ string) (ResponseSemanticCacheSQLExecutor, io.Closer, error) {
		return executor, nil, nil
	})
	defer SetResponseSemanticCacheSQLExecutorFactory(nil)
	repository, err := newPGVectorResponseSemanticCache("postgres://localhost/db")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if repository == nil {
		t.Fatal("expected non-nil repository")
	}
	if err := repository.Close(); err != nil {
		t.Fatalf("close failed: %v", err)
	}
	if executor.closed != 1 {
		t.Fatalf("expected executor closer to be called once, got %d", executor.closed)
	}
}

func TestNewPGVectorResponseSemanticCacheNilCloserNoCloserInterface(t *testing.T) {
	// 工厂返回 nil closer 且 executor 未实现 io.Closer 时，closeFunc 应为 no-op
	executor := &responseSemanticCacheSQLExecutorTestStub{}
	SetResponseSemanticCacheSQLExecutorFactory(func(_ string) (ResponseSemanticCacheSQLExecutor, io.Closer, error) {
		return executor, nil, nil
	})
	defer SetResponseSemanticCacheSQLExecutorFactory(nil)
	repository, err := newPGVectorResponseSemanticCache("postgres://localhost/db")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if err := repository.Close(); err != nil {
		t.Fatalf("close should be no-op, got %v", err)
	}
}

// closableExecutor 是一个实现 ResponseSemanticCacheSQLExecutor 和 io.Closer 的测试桩。
type closableExecutor struct {
	closed int
}

func (executor *closableExecutor) QueryContext(_ context.Context, _ string, _ ...any) (ResponseSemanticCacheSQLRows, error) {
	return nil, errors.New("not implemented")
}

func (executor *closableExecutor) ExecContext(_ context.Context, _ string, _ ...any) error {
	return errors.New("not implemented")
}

func (executor *closableExecutor) Close() error {
	executor.closed++
	return nil
}

// =============================================================================
// pgvectorResponseSemanticCache.Close 测试
// =============================================================================

func TestPGVectorResponseSemanticCacheCloseNil(t *testing.T) {
	// nil 仓库和 nil closeFunc 的 Close 应安全返回 nil
	tests := []struct {
		name       string
		repository *pgvectorResponseSemanticCache
	}{
		{name: "nil repository", repository: nil},
		{name: "nil closeFunc", repository: &pgvectorResponseSemanticCache{closeFunc: nil}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if err := test.repository.Close(); err != nil {
				t.Fatalf("expected nil error, got %v", err)
			}
		})
	}
}

func TestPGVectorResponseSemanticCacheCloseIdempotent(t *testing.T) {
	// Close 应幂等：多次调用只执行一次 closeFunc，返回相同结果
	calls := 0
	closeErr := errors.New("close failed")
	repository := &pgvectorResponseSemanticCache{
		closeFunc: func() error {
			calls++
			return closeErr
		},
	}
	firstErr := repository.Close()
	secondErr := repository.Close()
	thirdErr := repository.Close()
	if calls != 1 {
		t.Fatalf("closeFunc called %d times, want 1", calls)
	}
	if firstErr != closeErr || secondErr != closeErr || thirdErr != closeErr {
		t.Fatalf("close errors = %v, %v, %v; want all %v", firstErr, secondErr, thirdErr, closeErr)
	}
}

// =============================================================================
// pgvectorResponseSemanticCache.Lookup 错误路径测试
// =============================================================================

func TestPGVectorResponseSemanticCacheLookupNilRepository(t *testing.T) {
	// nil 仓库和 nil executor 的 Lookup 应返回 "repository is closed" 错误
	tests := []struct {
		name       string
		repository *pgvectorResponseSemanticCache
	}{
		{name: "nil repository", repository: nil},
		{name: "nil executor", repository: &pgvectorResponseSemanticCache{}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			result, err := test.repository.Lookup(context.Background(), ResponseSemanticCacheLookupRequest{
				Embedding: responseSemanticEmbedding("test"),
			})
			if err == nil || !strings.Contains(err.Error(), "repository is closed") {
				t.Fatalf("expected closed error, got %v", err)
			}
			if result.Exact != nil || result.Candidates != nil {
				t.Fatalf("expected empty result, got %#v", result)
			}
		})
	}
}

func TestPGVectorResponseSemanticCacheLookupInvalidEmbeddingDimensions(t *testing.T) {
	// 嵌入向量维度不匹配时应返回错误
	repository := &pgvectorResponseSemanticCache{executor: &responseSemanticCacheSQLExecutorTestStub{}}
	_, err := repository.Lookup(context.Background(), ResponseSemanticCacheLookupRequest{
		Embedding: make([]float64, 10), // 错误维度
	})
	if err == nil || !strings.Contains(err.Error(), "invalid dimensions") {
		t.Fatalf("expected dimensions error, got %v", err)
	}
}

func TestPGVectorResponseSemanticCacheLookupThresholdOutOfRange(t *testing.T) {
	// 相似度阈值超出 [0, 1] 范围时应返回错误
	repository := &pgvectorResponseSemanticCache{executor: &responseSemanticCacheSQLExecutorTestStub{}}
	embedding := responseSemanticEmbedding("threshold test")
	tests := []struct {
		name      string
		threshold float64
	}{
		{name: "negative", threshold: -0.1},
		{name: "above one", threshold: 1.5},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			_, err := repository.Lookup(context.Background(), ResponseSemanticCacheLookupRequest{
				Embedding: embedding, Threshold: test.threshold,
			})
			if err == nil || !strings.Contains(err.Error(), "threshold is out of range") {
				t.Fatalf("expected threshold error, got %v", err)
			}
		})
	}
}

func TestPGVectorResponseSemanticCacheLookupVectorTextError(t *testing.T) {
	// 嵌入向量包含 NaN 时，responseSemanticCacheVectorText 应报错
	repository := &pgvectorResponseSemanticCache{executor: &responseSemanticCacheSQLExecutorTestStub{}}
	embedding := make([]float64, responseSemanticCacheEmbeddingDimensions)
	embedding[0] = math.NaN()
	_, err := repository.Lookup(context.Background(), ResponseSemanticCacheLookupRequest{
		Embedding: embedding, Threshold: 0.5,
	})
	if err == nil {
		t.Fatal("expected vector text error for NaN embedding")
	}
}

func TestPGVectorResponseSemanticCacheLookupExactQueryError(t *testing.T) {
	// 精确查询失败时应包装错误并返回
	queryErr := errors.New("connection refused")
	executor := &scriptedSQLExecutor{
		queryResults: []struct {
			rows ResponseSemanticCacheSQLRows
			err  error
		}{{nil, queryErr}},
	}
	repository := &pgvectorResponseSemanticCache{executor: executor}
	_, err := repository.Lookup(context.Background(), ResponseSemanticCacheLookupRequest{
		Embedding: responseSemanticEmbedding("exact error"), Threshold: 0.5,
	})
	if err == nil || !strings.Contains(err.Error(), "lookup exact semantic cache entry") {
		t.Fatalf("expected wrapped exact query error, got %v", err)
	}
}

func TestPGVectorResponseSemanticCacheLookupExactScanError(t *testing.T) {
	// 精确匹配行扫描失败时应包装错误
	scanErr := errors.New("scan column mismatch")
	executor := &scriptedSQLExecutor{
		queryResults: []struct {
			rows ResponseSemanticCacheSQLRows
			err  error
		}{{&errorSQLRows{scanErr: scanErr}, nil}},
	}
	repository := &pgvectorResponseSemanticCache{executor: executor}
	_, err := repository.Lookup(context.Background(), ResponseSemanticCacheLookupRequest{
		Embedding: responseSemanticEmbedding("scan error"), Threshold: 0.5,
	})
	if err == nil || !strings.Contains(err.Error(), "scan exact semantic cache entry") {
		t.Fatalf("expected wrapped scan error, got %v", err)
	}
}

func TestPGVectorResponseSemanticCacheLookupExactRowsErr(t *testing.T) {
	// rows.Err() 返回错误时，scanPGVectorResponseSemanticCacheEntry 应传播该错误
	// 注意：需要 yielded=true 使 Next() 返回 false，从而进入 rows.Err() 检查路径
	rowsErr := errors.New("rows iteration error")
	executor := &scriptedSQLExecutor{
		queryResults: []struct {
			rows ResponseSemanticCacheSQLRows
			err  error
		}{{&errorSQLRows{errErr: rowsErr, yielded: true}, nil}},
	}
	repository := &pgvectorResponseSemanticCache{executor: executor}
	_, err := repository.Lookup(context.Background(), ResponseSemanticCacheLookupRequest{
		Embedding: responseSemanticEmbedding("rows err"), Threshold: 0.5,
	})
	if err == nil || !errors.Is(err, rowsErr) {
		t.Fatalf("expected rows.Err to propagate as %v, got %v", rowsErr, err)
	}
	if !strings.Contains(err.Error(), "scan exact semantic cache entry") {
		t.Fatalf("expected error to be wrapped with scan context, got %v", err)
	}
}

func TestPGVectorResponseSemanticCacheLookupMaxCandidatesZeroSkipsCandidateQuery(t *testing.T) {
	// MaxCandidates <= 0 时，即使没有精确命中也不应发起候选查询
	executor := &scriptedSQLExecutor{
		queryResults: []struct {
			rows ResponseSemanticCacheSQLRows
			err  error
		}{{&emptySQLRows{}, nil}},
	}
	repository := &pgvectorResponseSemanticCache{executor: executor}
	result, err := repository.Lookup(context.Background(), ResponseSemanticCacheLookupRequest{
		Embedding:      responseSemanticEmbedding("no candidates"),
		Threshold:      0.5,
		MaxCandidates:  0,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.Exact != nil || len(result.Candidates) != 0 {
		t.Fatalf("expected empty result, got %#v", result)
	}
	if executor.calls != 1 {
		t.Fatalf("expected 1 query (exact only), got %d", executor.calls)
	}
}

func TestPGVectorResponseSemanticCacheLookupCandidateQueryError(t *testing.T) {
	// 候选查询失败时应包装错误
	candidateErr := errors.New("candidate query failed")
	executor := &scriptedSQLExecutor{
		queryResults: []struct {
			rows ResponseSemanticCacheSQLRows
			err  error
		}{
			{&emptySQLRows{}, nil},         // 精确查询无命中
			{nil, candidateErr},             // 候选查询失败
		},
	}
	repository := &pgvectorResponseSemanticCache{executor: executor}
	_, err := repository.Lookup(context.Background(), ResponseSemanticCacheLookupRequest{
		Embedding:      responseSemanticEmbedding("candidate error"),
		Threshold:      0.5,
		MaxCandidates:  5,
	})
	if err == nil || !strings.Contains(err.Error(), "lookup semantic cache candidates") {
		t.Fatalf("expected wrapped candidate query error, got %v", err)
	}
}

func TestPGVectorResponseSemanticCacheLookupCandidateScanError(t *testing.T) {
	// 候选行扫描失败时应包装错误并关闭 rows
	scanErr := errors.New("candidate scan boom")
	executor := &scriptedSQLExecutor{
		queryResults: []struct {
			rows ResponseSemanticCacheSQLRows
			err  error
		}{
			{&emptySQLRows{}, nil},                                  // 精确查询无命中
			{&errorSQLRows{scanErr: scanErr}, nil},                  // 候选查询返回扫描错误行
		},
	}
	repository := &pgvectorResponseSemanticCache{executor: executor}
	_, err := repository.Lookup(context.Background(), ResponseSemanticCacheLookupRequest{
		Embedding:      responseSemanticEmbedding("candidate scan error"),
		Threshold:      0.5,
		MaxCandidates:  5,
	})
	if err == nil || !strings.Contains(err.Error(), "scan semantic cache candidate") {
		t.Fatalf("expected wrapped candidate scan error, got %v", err)
	}
}

func TestPGVectorResponseSemanticCacheLookupCandidateRowsErr(t *testing.T) {
	// 候选行迭代后 rows.Err() 返回错误时应包装
	rowsErr := errors.New("candidate iteration error")
	// 使用一个先返回一行（触发 Next）再在 Err() 报错的 rows
	rows := &candidateRowsErrStub{errErr: rowsErr}
	executor := &scriptedSQLExecutor{
		queryResults: []struct {
			rows ResponseSemanticCacheSQLRows
			err  error
		}{
			{&emptySQLRows{}, nil},  // 精确查询无命中
			{rows, nil},              // 候选查询返回行
		},
	}
	repository := &pgvectorResponseSemanticCache{executor: executor}
	_, err := repository.Lookup(context.Background(), ResponseSemanticCacheLookupRequest{
		Embedding:      responseSemanticEmbedding("candidate rows err"),
		Threshold:      0.5,
		MaxCandidates:  5,
	})
	if err == nil || !strings.Contains(err.Error(), "iterate semantic cache candidates") {
		t.Fatalf("expected wrapped rows.Err error, got %v", err)
	}
}

// candidateRowsErrStub 在 Next() 返回 true 一次后，Scan 返回有效行，
// 但 Err() 返回预设错误，用于测试候选行迭代后的 rows.Err() 路径。
type candidateRowsErrStub struct {
	errErr  error
	yielded bool
}

func (rows *candidateRowsErrStub) Next() bool {
	if rows.yielded {
		return false
	}
	rows.yielded = true
	return true
}

func (rows *candidateRowsErrStub) Scan(dest ...any) error {
	// scanPGVectorResponseSemanticCacheCandidate 需要 20 列
	now := time.Now().UTC()
	scope := responseSemanticCacheScope{
		WorkspaceID: "ws_test", Model: "m", Provider: "p", ChannelID: "c",
		UpstreamModel: "u", Capability: "cap", PromptID: "pid", PromptVersion: 1,
		PromptChecksum: "pc", GuardPolicyVersion: "gv", DataClassification: "C2",
		OutputSignature: "os", Region: "global",
	}
	embedding := responseSemanticEmbedding("candidate row")
	vector, _ := responseSemanticCacheVectorText(embedding)
	row := responseSemanticCacheSQLCandidateRow("key-1", responseSemanticCacheEntry{
		Text: "candidate", CompletionTokens: 1, CreatedAt: now, ExpiresAt: now.Add(time.Minute),
		Scope: scope, Embedding: embedding,
	}, vector, 0.99)
	if len(dest) != len(row) {
		return errors.New("column count mismatch")
	}
	return (&responseSemanticCacheSQLRowsStub{current: row}).Scan(dest...)
}

func (rows *candidateRowsErrStub) Close() error { return nil }
func (rows *candidateRowsErrStub) Err() error   { return rows.errErr }

func TestPGVectorResponseSemanticCacheLookupDefaultsNowWhenZero(t *testing.T) {
	// Now 为零值时，Lookup 应自动填充为 time.Now()
	executor := &scriptedSQLExecutor{
		queryResults: []struct {
			rows ResponseSemanticCacheSQLRows
			err  error
		}{{&emptySQLRows{}, nil}},
	}
	repository := &pgvectorResponseSemanticCache{executor: executor}
	before := time.Now()
	_, err := repository.Lookup(context.Background(), ResponseSemanticCacheLookupRequest{
		Embedding:     responseSemanticEmbedding("default now"),
		Threshold:     0.5,
		MaxCandidates: 0,
		// Now 故意留零值
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	after := time.Now()
	// 验证 Now 被填充（通过检查查询参数中的时间戳）
	if len(executor.execArgs) < 15 {
		t.Fatalf("expected at least 15 query args, got %d", len(executor.execArgs))
	}
	nowArg, ok := executor.execArgs[14].(time.Time)
	if !ok {
		t.Fatalf("expected 15th arg to be time.Time, got %T", executor.execArgs[14])
	}
	if nowArg.Before(before) || nowArg.After(after) {
		t.Fatalf("default Now = %v, expected between %v and %v", nowArg, before, after)
	}
}

// =============================================================================
// pgvectorResponseSemanticCache.Put 错误路径测试
// =============================================================================

func TestPGVectorResponseSemanticCachePutNilRepository(t *testing.T) {
	// nil 仓库和 nil executor 的 Put 应返回 "repository is closed" 错误
	tests := []struct {
		name       string
		repository *pgvectorResponseSemanticCache
	}{
		{name: "nil repository", repository: nil},
		{name: "nil executor", repository: &pgvectorResponseSemanticCache{}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			err := test.repository.Put(context.Background(), "key", ResponseSemanticCacheEntry{
				Text: "text", CompletionTokens: 1,
				CreatedAt: time.Now(), ExpiresAt: time.Now().Add(time.Minute),
				Embedding: responseSemanticEmbedding("test"),
			})
			if err == nil || !strings.Contains(err.Error(), "repository is closed") {
				t.Fatalf("expected closed error, got %v", err)
			}
		})
	}
}

func TestPGVectorResponseSemanticCachePutInvalidKeyOrText(t *testing.T) {
	// 空 key、空白 key、空文本、空白文本、超长文本都应被拒绝
	executor := &responseSemanticCacheSQLExecutorTestStub{}
	repository := &pgvectorResponseSemanticCache{executor: executor}
	now := time.Now()
	validEmbedding := responseSemanticEmbedding("valid")
	tests := []struct {
		name  string
		key   string
		text  string
	}{
		{name: "empty key", key: "", text: "text"},
		{name: "whitespace key", key: "   ", text: "text"},
		{name: "empty text", key: "key", text: ""},
		{name: "whitespace text", key: "key", text: "   "},
		{name: "text too long", key: "key", text: strings.Repeat("a", responseSemanticCacheMaxText+1)},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			err := repository.Put(context.Background(), test.key, ResponseSemanticCacheEntry{
				Text: test.text, CompletionTokens: 1,
				CreatedAt: now, ExpiresAt: now.Add(time.Minute),
				Embedding: validEmbedding,
			})
			if err == nil || !strings.Contains(err.Error(), "invalid text or key") {
				t.Fatalf("expected invalid text/key error, got %v", err)
			}
		})
	}
}

func TestPGVectorResponseSemanticCachePutInvalidTimestampsOrTokens(t *testing.T) {
	// 负 completion tokens、零时间戳、过期时间不晚于创建时间都应被拒绝
	executor := &responseSemanticCacheSQLExecutorTestStub{}
	repository := &pgvectorResponseSemanticCache{executor: executor}
	validEmbedding := responseSemanticEmbedding("valid")
	now := time.Now()
	tests := []struct {
		name      string
		tokens    int
		createdAt time.Time
		expiresAt time.Time
	}{
		{name: "negative tokens", tokens: -1, createdAt: now, expiresAt: now.Add(time.Minute)},
		{name: "zero createdAt", tokens: 1, createdAt: time.Time{}, expiresAt: now.Add(time.Minute)},
		{name: "zero expiresAt", tokens: 1, createdAt: now, expiresAt: time.Time{}},
		{name: "expires before created", tokens: 1, createdAt: now, expiresAt: now.Add(-time.Minute)},
		{name: "expires equals created", tokens: 1, createdAt: now, expiresAt: now},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			err := repository.Put(context.Background(), "key", ResponseSemanticCacheEntry{
				Text: "text", CompletionTokens: test.tokens,
				CreatedAt: test.createdAt, ExpiresAt: test.expiresAt,
				Embedding: validEmbedding,
			})
			if err == nil || !strings.Contains(err.Error(), "invalid timestamps or completion tokens") {
				t.Fatalf("expected timestamp/tokens error, got %v", err)
			}
		})
	}
}

func TestPGVectorResponseSemanticCachePutVectorTextError(t *testing.T) {
	// 嵌入向量包含 Inf 时，responseSemanticCacheVectorText 应报错
	executor := &responseSemanticCacheSQLExecutorTestStub{}
	repository := &pgvectorResponseSemanticCache{executor: executor}
	now := time.Now()
	embedding := make([]float64, responseSemanticCacheEmbeddingDimensions)
	embedding[0] = math.Inf(1)
	err := repository.Put(context.Background(), "key", ResponseSemanticCacheEntry{
		Text: "text", CompletionTokens: 1,
		CreatedAt: now, ExpiresAt: now.Add(time.Minute),
		Embedding: embedding,
	})
	if err == nil {
		t.Fatal("expected vector text error for Inf embedding")
	}
}

func TestPGVectorResponseSemanticCachePutExecError(t *testing.T) {
	// ExecContext 失败时应包装错误
	execErr := errors.New("exec boom")
	executor := &scriptedSQLExecutor{execErr: execErr}
	repository := &pgvectorResponseSemanticCache{executor: executor}
	now := time.Now()
	err := repository.Put(context.Background(), "key", ResponseSemanticCacheEntry{
		Text: "text", CompletionTokens: 1,
		CreatedAt: now, ExpiresAt: now.Add(time.Minute),
		Embedding: responseSemanticEmbedding("exec error"),
	})
	if err == nil || !strings.Contains(err.Error(), "put semantic cache entry") {
		t.Fatalf("expected wrapped exec error, got %v", err)
	}
}

// =============================================================================
// disabledResponseSemanticCacheRepository 测试
// =============================================================================

func TestDisabledResponseSemanticCacheRepositoryLookupReturnsEmpty(t *testing.T) {
	// 禁用的仓库 Lookup 应返回空结果和 nil 错误（fail-closed）
	var repository disabledResponseSemanticCacheRepository
	result, err := repository.Lookup(context.Background(), ResponseSemanticCacheLookupRequest{})
	if err != nil {
		t.Fatalf("expected nil error, got %v", err)
	}
	if result.Exact != nil || result.Candidates != nil {
		t.Fatalf("expected empty result, got %#v", result)
	}
}

func TestDisabledResponseSemanticCacheRepositoryPutReturnsNil(t *testing.T) {
	// 禁用的仓库 Put 应静默成功（fail-closed）
	var repository disabledResponseSemanticCacheRepository
	if err := repository.Put(context.Background(), "key", ResponseSemanticCacheEntry{}); err != nil {
		t.Fatalf("expected nil error, got %v", err)
	}
}

// =============================================================================
// responseSemanticCachePGVectorWorkspaceAllowlistConfigured 测试
// =============================================================================

func TestResponseSemanticCachePGVectorWorkspaceAllowlistConfigured(t *testing.T) {
	// 验证 pgvector 工作区白名单配置检查逻辑
	tests := []struct {
		name     string
		envValue string
		want     bool
	}{
		{name: "empty env", envValue: "", want: false},
		{name: "wildcard only", envValue: "*", want: false},
		{name: "whitespace only", envValue: "   ", want: false},
		{name: "single valid workspace", envValue: "ws_alpha", want: true},
		{name: "wildcard with workspace", envValue: "*,ws_alpha", want: true},
		{name: "workspace with whitespace", envValue: "  ws_alpha  ", want: true},
		{name: "empty items mixed with workspace", envValue: ",,ws_alpha,", want: true},
		{name: "only empty items", envValue: ",,,", want: false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			t.Setenv(responseSemanticCachePGVectorWorkspacesEnv, test.envValue)
			if got := responseSemanticCachePGVectorWorkspaceAllowlistConfigured(); got != test.want {
				t.Fatalf("responseSemanticCachePGVectorWorkspaceAllowlistConfigured() = %v, want %v", got, test.want)
			}
		})
	}
}

// =============================================================================
// responseSemanticCacheExactArgs 测试
// =============================================================================

func TestResponseSemanticCacheExactArgsOrder(t *testing.T) {
	// 验证精确查询参数顺序：Key, WorkspaceID, Model, Provider, ChannelID,
	// UpstreamModel, Capability, PromptID, PromptVersion, PromptChecksum,
	// GuardPolicyVersion, DataClassification, OutputSignature, Region, Now
	now := time.Now().UTC()
	scope := responseSemanticCacheScope{
		WorkspaceID: "ws_1", Model: "model_1", Provider: "prov_1", ChannelID: "chn_1",
		UpstreamModel: "up_1", Capability: "cap_1", PromptID: "pid_1", PromptVersion: 42,
		PromptChecksum: "pc_1", GuardPolicyVersion: "gp_1", DataClassification: "C2",
		OutputSignature: "sig_1", Region: "us_1",
	}
	args := responseSemanticCacheExactArgs(ResponseSemanticCacheLookupRequest{
		Key: "cache_key_1", Scope: scope, Now: now,
	})
	if len(args) != 15 {
		t.Fatalf("args count = %d, want 15", len(args))
	}
	expected := []any{
		"cache_key_1", "ws_1", "model_1", "prov_1", "chn_1",
		"up_1", "cap_1", "pid_1", 42, "pc_1",
		"gp_1", "C2", "sig_1", "us_1", now,
	}
	for index, want := range expected {
		if args[index] != want {
			t.Fatalf("arg[%d] = %v (%T), want %v (%T)", index, args[index], args[index], want, want)
		}
	}
}

// =============================================================================
// scanPGVectorResponseSemanticCacheEntry 测试（直接调用）
// =============================================================================

func TestScanPGVectorResponseSemanticCacheEntryEmptyRows(t *testing.T) {
	// 无数据行时 scanPGVectorResponseSemanticCacheEntry 应返回 nil candidate, nil error
	result, err := scanPGVectorResponseSemanticCacheEntry(&emptySQLRows{}, "any-key")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result != nil {
		t.Fatalf("expected nil candidate, got %#v", result)
	}
}

func TestScanPGVectorResponseSemanticCacheEntryRowsErr(t *testing.T) {
	// rows.Err() 返回错误时，scanPGVectorResponseSemanticCacheEntry 应传播该错误
	rowsErr := errors.New("rows error")
	rows := &errorSQLRows{errErr: rowsErr}
	// errorSQLRows.Next() returns true once then false; but we want Next() to return false
	// so that scanPGVectorResponseSemanticCacheEntry hits the rows.Err() path.
	// Actually, errorSQLRows.Next() returns true first time. We need a rows that returns false on Next.
	rows.yielded = true // force Next() to return false
	result, err := scanPGVectorResponseSemanticCacheEntry(rows, "key")
	if err == nil || err != rowsErr {
		t.Fatalf("expected rows.Err to propagate, got %v", err)
	}
	if result != nil {
		t.Fatalf("expected nil candidate, got %#v", result)
	}
}

func TestScanPGVectorResponseSemanticCacheEntryScanError(t *testing.T) {
	// Scan 返回错误时应传播
	scanErr := errors.New("scan failure")
	rows := &errorSQLRows{scanErr: scanErr}
	_, err := scanPGVectorResponseSemanticCacheEntry(rows, "key")
	if err == nil || err != scanErr {
		t.Fatalf("expected scan error to propagate, got %v", err)
	}
}

func TestScanPGVectorResponseSemanticCacheEntryParseError(t *testing.T) {
	// 嵌入向量文本格式错误时，解析应失败
	now := time.Now().UTC().Truncate(time.Microsecond)
	scope := responseSemanticCacheScope{WorkspaceID: "ws", Model: "m"}
	// 构造一个行，其中 embedding 列是无效文本
	executor := &responseSemanticCacheSQLExecutorStub{
		exactRows: [][]any{{
			"text", 1, now, now.Add(time.Minute),
			scope.WorkspaceID, scope.Model, scope.Provider, scope.ChannelID,
			scope.UpstreamModel, scope.Capability, scope.PromptID, scope.PromptVersion,
			scope.PromptChecksum, scope.GuardPolicyVersion, scope.DataClassification,
			scope.OutputSignature, scope.Region, "not_a_vector",
		}},
	}
	rows, err := executor.QueryContext(context.Background(), "cache_key = $1", "k")
	if err != nil {
		t.Fatal(err)
	}
	_, err = scanPGVectorResponseSemanticCacheEntry(rows, "key")
	if err == nil {
		t.Fatal("expected parse error for invalid vector text")
	}
}

// =============================================================================
// scanPGVectorResponseSemanticCacheCandidate 测试（直接调用）
// =============================================================================

func TestScanPGVectorResponseSemanticCacheCandidateParseError(t *testing.T) {
	// 候选行中嵌入向量文本格式错误时，解析应失败
	now := time.Now().UTC().Truncate(time.Microsecond)
	scope := responseSemanticCacheScope{WorkspaceID: "ws", Model: "m"}
	// 候选行格式：key, entry_columns..., embedding, similarity
	executor := &responseSemanticCacheSQLExecutorStub{
		candidateRows: [][]any{{
			"key-1", "text", 1, now, now.Add(time.Minute),
			scope.WorkspaceID, scope.Model, scope.Provider, scope.ChannelID,
			scope.UpstreamModel, scope.Capability, scope.PromptID, scope.PromptVersion,
			scope.PromptChecksum, scope.GuardPolicyVersion, scope.DataClassification,
			scope.OutputSignature, scope.Region, "INVALID_VECTOR", 0.99,
		}},
	}
	rows, err := executor.QueryContext(context.Background(), "candidate query", "v")
	if err != nil {
		t.Fatal(err)
	}
	if !rows.Next() {
		t.Fatal("expected at least one row")
	}
	_, _, err = scanPGVectorResponseSemanticCacheCandidate(rows)
	if err == nil {
		t.Fatal("expected parse error for invalid candidate vector text")
	}
}

// =============================================================================
// 接口实现断言测试
// =============================================================================

func TestPGVectorResponseSemanticCacheImplementsRepository(t *testing.T) {
	// 编译期断言：pgvectorResponseSemanticCache 实现 ResponseSemanticCacheRepository 接口
	var _ ResponseSemanticCacheRepository = (*pgvectorResponseSemanticCache)(nil)
}

func TestDisabledResponseSemanticCacheRepositoryImplementsRepository(t *testing.T) {
	// 编译期断言：disabledResponseSemanticCacheRepository 实现 ResponseSemanticCacheRepository 接口
	var _ ResponseSemanticCacheRepository = (*disabledResponseSemanticCacheRepository)(nil)
}
