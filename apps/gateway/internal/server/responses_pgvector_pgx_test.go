//go:build pgx

package server

import (
	"context"
	"database/sql"
	"strings"
	"testing"
	"time"
)

// =============================================================================
// pgx 适配器初始化与工厂注册测试
// =============================================================================

func TestPGXInitRegistersSQLExecutorFactory(t *testing.T) {
	// init() 应已注册 pgx 工厂，使得 responseSemanticCacheSQLExecutorFactoryValue() 返回非 nil
	factory := responseSemanticCacheSQLExecutorFactoryValue()
	if factory == nil {
		t.Fatal("pgx init() did not register SQL executor factory")
	}
}

func TestPGXFactoryReturnsExecutorAndCloser(t *testing.T) {
	// newResponseSemanticCachePGXExecutor 应返回非 nil executor 和 closer
	// sql.Open 是惰性的，即使 URL 无效也不会报错
	executor, closer, err := newResponseSemanticCachePGXExecutor("postgres://user:pass@127.0.0.1:1/invalid")
	if err != nil {
		t.Fatalf("unexpected error from lazy sql.Open: %v", err)
	}
	if executor == nil {
		t.Fatal("expected non-nil executor")
	}
	if closer == nil {
		t.Fatal("expected non-nil closer")
	}
	if err := closer.Close(); err != nil {
		t.Fatalf("closer.Close() failed: %v", err)
	}
}

func TestPGXFactoryReturnsSQLDBTypes(t *testing.T) {
	// 验证返回的类型符合预期：executor 是 *responseSemanticCacheSQLDBExecutor，closer 是 *sql.DB
	executor, closer, err := newResponseSemanticCachePGXExecutor("postgres://user:pass@127.0.0.1:1/invalid")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	defer closer.Close()

	if _, ok := executor.(*responseSemanticCacheSQLDBExecutor); !ok {
		t.Fatalf("executor type = %T, want *responseSemanticCacheSQLDBExecutor", executor)
	}
	if _, ok := closer.(*sql.DB); !ok {
		t.Fatalf("closer type = %T, want *sql.DB", closer)
	}
}

// =============================================================================
// pgx 执行器查询/执行失败测试
// =============================================================================

func TestPGXExecutorQueryContextFailsWithInvalidConnection(t *testing.T) {
	// 无效连接的 QueryContext 应在短超时内失败
	executor, closer, err := newResponseSemanticCachePGXExecutor("postgres://user:pass@127.0.0.1:1/invalid")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	defer closer.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	rows, err := executor.QueryContext(ctx, "SELECT 1")
	if err == nil {
		if rows != nil {
			_ = rows.Close()
		}
		t.Fatal("expected QueryContext to fail with invalid connection, got nil error")
	}
}

func TestPGXExecutorExecContextFailsWithInvalidConnection(t *testing.T) {
	// 无效连接的 ExecContext 应在短超时内失败
	executor, closer, err := newResponseSemanticCachePGXExecutor("postgres://user:pass@127.0.0.1:1/invalid")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	defer closer.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	err = executor.ExecContext(ctx, "INSERT INTO test VALUES (1)")
	if err == nil {
		t.Fatal("expected ExecContext to fail with invalid connection, got nil error")
	}
}

// =============================================================================
// pgx 行包装器测试（通过 mock DB 验证代理行为）
// =============================================================================

func TestPGXExecutorImplementsSQLExecutorInterface(t *testing.T) {
	// 编译期断言：responseSemanticCacheSQLDBExecutor 实现 ResponseSemanticCacheSQLExecutor 接口
	var _ ResponseSemanticCacheSQLExecutor = (*responseSemanticCacheSQLDBExecutor)(nil)
}

func TestPGXRowsImplementsSQLRowsInterface(t *testing.T) {
	// 编译期断言：responseSemanticCacheSQLDBRows 实现 ResponseSemanticCacheSQLRows 接口
	var _ ResponseSemanticCacheSQLRows = (*responseSemanticCacheSQLDBRows)(nil)
}

// =============================================================================
// pgx 工厂与 newPGVectorResponseSemanticCache 集成测试
// =============================================================================

func TestPGXFactoryIntegrationWithRepositoryConstruction(t *testing.T) {
	// 使用 pgx 工厂构造 pgvectorResponseSemanticCache 应成功（惰性连接）
	// 注意：init() 已注册工厂，但前面的测试可能已清空，此处显式恢复
	originalFactory := responseSemanticCacheSQLExecutorFactoryValue()
	defer SetResponseSemanticCacheSQLExecutorFactory(originalFactory)
	if originalFactory == nil {
		SetResponseSemanticCacheSQLExecutorFactory(newResponseSemanticCachePGXExecutor)
	}

	repository, err := newPGVectorResponseSemanticCache("postgres://user:pass@127.0.0.1:1/invalid")
	if err != nil {
		t.Fatalf("repository construction failed: %v", err)
	}
	if repository == nil {
		t.Fatal("expected non-nil repository")
	}
	// Close 应正常关闭惰性连接池
	if err := repository.Close(); err != nil {
		t.Fatalf("repository.Close() failed: %v", err)
	}
	// 再次 Close 应幂等
	if err := repository.Close(); err != nil {
		t.Fatalf("second repository.Close() should be idempotent, got: %v", err)
	}
}

func TestPGXRepositoryLookupFailsClosedWithoutDatabase(t *testing.T) {
	// 无数据库连接时，Lookup 应返回错误而非 panic（fail-closed）
	originalFactory := responseSemanticCacheSQLExecutorFactoryValue()
	defer SetResponseSemanticCacheSQLExecutorFactory(originalFactory)
	if originalFactory == nil {
		SetResponseSemanticCacheSQLExecutorFactory(newResponseSemanticCachePGXExecutor)
	}

	repository, err := newPGVectorResponseSemanticCache("postgres://user:pass@127.0.0.1:1/invalid")
	if err != nil {
		t.Fatalf("repository construction failed: %v", err)
	}
	defer repository.Close()

	embedding := responseSemanticEmbedding("pgx integration lookup")
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	result, err := repository.Lookup(ctx, ResponseSemanticCacheLookupRequest{
		Embedding: embedding, Threshold: 0.5, MaxCandidates: 0,
	})
	if err == nil {
		t.Fatal("expected Lookup to fail without database connection")
	}
	// 错误应包含查询上下文信息
	if !strings.Contains(err.Error(), "lookup exact semantic cache entry") {
		t.Fatalf("expected wrapped lookup error, got %v", err)
	}
	if result.Exact != nil || result.Candidates != nil {
		t.Fatalf("expected empty result on error, got %#v", result)
	}
}

func TestPGXRepositoryPutFailsClosedWithoutDatabase(t *testing.T) {
	// 无数据库连接时，Put 应返回错误而非 panic（fail-closed）
	originalFactory := responseSemanticCacheSQLExecutorFactoryValue()
	defer SetResponseSemanticCacheSQLExecutorFactory(originalFactory)
	if originalFactory == nil {
		SetResponseSemanticCacheSQLExecutorFactory(newResponseSemanticCachePGXExecutor)
	}

	repository, err := newPGVectorResponseSemanticCache("postgres://user:pass@127.0.0.1:1/invalid")
	if err != nil {
		t.Fatalf("repository construction failed: %v", err)
	}
	defer repository.Close()

	now := time.Now()
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	err = repository.Put(ctx, "test-key", ResponseSemanticCacheEntry{
		Text: "test output", CompletionTokens: 1,
		CreatedAt: now, ExpiresAt: now.Add(time.Minute),
		Embedding: responseSemanticEmbedding("pgx integration put"),
	})
	if err == nil {
		t.Fatal("expected Put to fail without database connection")
	}
	if !strings.Contains(err.Error(), "put semantic cache entry") {
		t.Fatalf("expected wrapped put error, got %v", err)
	}
}

// =============================================================================
// pgx 空数据库 URL 错误测试
// =============================================================================

func TestPGXFactoryWithEmptyURL(t *testing.T) {
	// 空 URL 调用工厂不应 panic（sql.Open 仍为惰性）
	// 但 newPGVectorResponseSemanticCache 应在空 URL 时提前报错
	if _, err := newPGVectorResponseSemanticCache(""); err == nil {
		t.Fatal("expected error for empty URL")
	}
	if _, err := newPGVectorResponseSemanticCache("   "); err == nil {
		t.Fatal("expected error for whitespace-only URL")
	}
}
