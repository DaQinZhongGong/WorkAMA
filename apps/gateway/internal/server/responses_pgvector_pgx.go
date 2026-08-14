//go:build pgx

package server

import (
	"context"
	"database/sql"
	"fmt"
	"io"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"
)

// The factory remains an injection boundary, while the bundled pgx stdlib
// adapter gives the gateway binary a production default. Deployments can
// replace it before constructing a Server when they already own a pool.
func init() {
	SetResponseSemanticCacheSQLExecutorFactory(newResponseSemanticCachePGXExecutor)
}

type responseSemanticCacheSQLDBExecutor struct {
	db *sql.DB
}

type responseSemanticCacheSQLDBRows struct {
	rows *sql.Rows
}

func newResponseSemanticCachePGXExecutor(databaseURL string) (ResponseSemanticCacheSQLExecutor, io.Closer, error) {
	db, err := sql.Open("pgx", databaseURL)
	if err != nil {
		return nil, nil, fmt.Errorf("open pgx connection pool: %w", err)
	}
	db.SetMaxOpenConns(4)
	db.SetMaxIdleConns(4)
	db.SetConnMaxIdleTime(5 * time.Minute)
	db.SetConnMaxLifetime(30 * time.Minute)
	return &responseSemanticCacheSQLDBExecutor{db: db}, db, nil
}

func (executor *responseSemanticCacheSQLDBExecutor) QueryContext(ctx context.Context, query string, args ...any) (ResponseSemanticCacheSQLRows, error) {
	rows, err := executor.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	return &responseSemanticCacheSQLDBRows{rows: rows}, nil
}

func (executor *responseSemanticCacheSQLDBExecutor) ExecContext(ctx context.Context, query string, args ...any) error {
	_, err := executor.db.ExecContext(ctx, query, args...)
	return err
}

func (rows *responseSemanticCacheSQLDBRows) Next() bool { return rows.rows.Next() }

func (rows *responseSemanticCacheSQLDBRows) Scan(dest ...any) error { return rows.rows.Scan(dest...) }

func (rows *responseSemanticCacheSQLDBRows) Close() error { return rows.rows.Close() }

func (rows *responseSemanticCacheSQLDBRows) Err() error { return rows.rows.Err() }

var _ ResponseSemanticCacheSQLExecutor = (*responseSemanticCacheSQLDBExecutor)(nil)
