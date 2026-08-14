package server

import (
	"context"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

const responseSemanticCacheDatabaseURLEnv = "DATABASE_URL"

const responseSemanticCachePGVectorColumns = `
	completion_text, completion_tokens, created_at, expires_at,
	workspace_id, model, provider, channel_id, upstream_model, capability,
	prompt_id, prompt_version, prompt_checksum, guard_policy_version,
	data_classification, output_signature, region, embedding::text`

const responseSemanticCachePGVectorExactQuery = `SELECT` + responseSemanticCachePGVectorColumns + `
FROM gw_response_semantic_cache
WHERE cache_key = $1
  AND workspace_id = $2
  AND model = $3
  AND provider = $4
  AND channel_id = $5
  AND upstream_model = $6
  AND capability = $7
  AND prompt_id = $8
  AND prompt_version = $9
  AND prompt_checksum = $10
  AND guard_policy_version = $11
  AND data_classification = $12
  AND output_signature = $13
  AND region = $14
  AND expires_at > $15
LIMIT 1`

const responseSemanticCachePGVectorCandidateQuery = `SELECT
	cache_key,` + responseSemanticCachePGVectorColumns + `,
	1 - (embedding <=> $1::vector) AS similarity
FROM gw_response_semantic_cache
WHERE workspace_id = $2
  AND model = $3
  AND provider = $4
  AND channel_id = $5
  AND upstream_model = $6
  AND capability = $7
  AND prompt_id = $8
  AND prompt_version = $9
  AND prompt_checksum = $10
  AND guard_policy_version = $11
  AND data_classification = $12
  AND output_signature = $13
  AND region = $14
  AND expires_at > $15
  AND embedding <=> $1::vector <= 1 - $16
ORDER BY embedding <=> $1::vector, cache_key
LIMIT $17`

const responseSemanticCachePGVectorPutQuery = `INSERT INTO gw_response_semantic_cache (
	cache_key, completion_text, completion_tokens, workspace_id, model, provider,
	channel_id, upstream_model, capability, prompt_id, prompt_version,
	prompt_checksum, guard_policy_version, data_classification, output_signature,
	region, embedding, created_at, expires_at
) VALUES (
	$1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
	$17::vector, $18, $19
)
ON CONFLICT (cache_key) DO UPDATE SET
	completion_text = EXCLUDED.completion_text,
	completion_tokens = EXCLUDED.completion_tokens,
	workspace_id = EXCLUDED.workspace_id,
	model = EXCLUDED.model,
	provider = EXCLUDED.provider,
	channel_id = EXCLUDED.channel_id,
	upstream_model = EXCLUDED.upstream_model,
	capability = EXCLUDED.capability,
	prompt_id = EXCLUDED.prompt_id,
	prompt_version = EXCLUDED.prompt_version,
	prompt_checksum = EXCLUDED.prompt_checksum,
	guard_policy_version = EXCLUDED.guard_policy_version,
	data_classification = EXCLUDED.data_classification,
	output_signature = EXCLUDED.output_signature,
	region = EXCLUDED.region,
	embedding = EXCLUDED.embedding,
	created_at = EXCLUDED.created_at,
	expires_at = EXCLUDED.expires_at`

type pgvectorResponseSemanticCache struct {
	executor  ResponseSemanticCacheSQLExecutor
	closeFunc func() error
	closeOnce sync.Once
	closeErr  error
}

// ResponseSemanticCacheSQLExecutorFactory injects the deployment's PostgreSQL
// pool/driver without making the Gateway vendor a second database stack.
type ResponseSemanticCacheSQLExecutorFactory func(string) (ResponseSemanticCacheSQLExecutor, io.Closer, error)

var responseSemanticCacheSQLExecutorFactory struct {
	mu    sync.RWMutex
	value ResponseSemanticCacheSQLExecutorFactory
}

// SetResponseSemanticCacheSQLExecutorFactory installs the production SQL pool
// adapter. A nil factory leaves persistence fail-closed and keeps memory cache
// behavior unchanged.
func SetResponseSemanticCacheSQLExecutorFactory(factory ResponseSemanticCacheSQLExecutorFactory) {
	responseSemanticCacheSQLExecutorFactory.mu.Lock()
	responseSemanticCacheSQLExecutorFactory.value = factory
	responseSemanticCacheSQLExecutorFactory.mu.Unlock()
}

func responseSemanticCacheSQLExecutorFactoryValue() ResponseSemanticCacheSQLExecutorFactory {
	responseSemanticCacheSQLExecutorFactory.mu.RLock()
	defer responseSemanticCacheSQLExecutorFactory.mu.RUnlock()
	return responseSemanticCacheSQLExecutorFactory.value
}

type disabledResponseSemanticCacheRepository struct{}

func (disabledResponseSemanticCacheRepository) Lookup(context.Context, ResponseSemanticCacheLookupRequest) (ResponseSemanticCacheLookupResult, error) {
	return ResponseSemanticCacheLookupResult{}, nil
}

func (disabledResponseSemanticCacheRepository) Put(context.Context, string, ResponseSemanticCacheEntry) error {
	return nil
}

var responseSemanticCacheDisabledRepository responseSemanticCacheRepository = disabledResponseSemanticCacheRepository{}

func responseSemanticCachePGVectorConfigured() bool {
	return responseSemanticCachePGVectorEnabled() &&
		responseSemanticCacheEnabled() &&
		strings.TrimSpace(os.Getenv(responseSemanticCacheDatabaseURLEnv)) != "" &&
		responseSemanticCachePGVectorWorkspaceAllowlistConfigured()
}

func responseSemanticCachePGVectorWorkspaceAllowlistConfigured() bool {
	for _, item := range strings.Split(os.Getenv(responseSemanticCachePGVectorWorkspacesEnv), ",") {
		if strings.TrimSpace(item) != "" && strings.TrimSpace(item) != "*" {
			return true
		}
	}
	return false
}

func (store *responseRegistry) ensureProductionSemanticCacheRepository() {
	if store == nil || !responseSemanticCachePGVectorConfigured() {
		return
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.semanticCacheRepository != nil {
		return
	}
	repository, err := newPGVectorResponseSemanticCache(os.Getenv(responseSemanticCacheDatabaseURLEnv))
	if err != nil {
		if store.logger != nil {
			store.logger.Warn("response semantic cache pgvector initialization failed; using in-memory fallback", "error", err)
		}
		return
	}
	store.semanticCacheRepository = repository
}

func newPGVectorResponseSemanticCache(databaseURL string) (*pgvectorResponseSemanticCache, error) {
	databaseURL = strings.TrimSpace(databaseURL)
	if databaseURL == "" {
		return nil, errors.New("DATABASE_URL is empty")
	}
	factory := responseSemanticCacheSQLExecutorFactoryValue()
	if factory == nil {
		return nil, errors.New("PostgreSQL driver is not injected")
	}
	executor, closer, err := factory(databaseURL)
	if err != nil {
		return nil, fmt.Errorf("open PostgreSQL connection pool: %w", err)
	}
	if executor == nil {
		if closer != nil {
			_ = closer.Close()
		}
		return nil, errors.New("PostgreSQL driver factory returned a nil executor")
	}
	if closer == nil {
		if candidate, ok := executor.(io.Closer); ok {
			closer = candidate
		}
	}
	closeFunc := func() error { return nil }
	if closer != nil {
		closeFunc = closer.Close
	}
	return &pgvectorResponseSemanticCache{
		executor:  executor,
		closeFunc: closeFunc,
	}, nil
}

func (repository *pgvectorResponseSemanticCache) Close() error {
	if repository == nil || repository.closeFunc == nil {
		return nil
	}
	repository.closeOnce.Do(func() { repository.closeErr = repository.closeFunc() })
	return repository.closeErr
}

func (repository *pgvectorResponseSemanticCache) Lookup(ctx context.Context, query ResponseSemanticCacheLookupRequest) (ResponseSemanticCacheLookupResult, error) {
	if repository == nil || repository.executor == nil {
		return ResponseSemanticCacheLookupResult{}, errors.New("pgvector repository is closed")
	}
	if len(query.Embedding) != responseSemanticCacheEmbeddingDimensions {
		return ResponseSemanticCacheLookupResult{}, errors.New("semantic cache embedding has invalid dimensions")
	}
	if query.Now.IsZero() {
		query.Now = time.Now()
	}
	if query.Threshold < 0 || query.Threshold > 1 {
		return ResponseSemanticCacheLookupResult{}, errors.New("semantic cache threshold is out of range")
	}
	vector, err := responseSemanticCacheVectorText(query.Embedding)
	if err != nil {
		return ResponseSemanticCacheLookupResult{}, err
	}

	result := ResponseSemanticCacheLookupResult{}
	rows, err := repository.executor.QueryContext(ctx, responseSemanticCachePGVectorExactQuery, responseSemanticCacheExactArgs(query)...)
	if err != nil {
		return result, fmt.Errorf("lookup exact semantic cache entry: %w", err)
	}
	exact, err := scanPGVectorResponseSemanticCacheEntry(rows, "")
	if closeErr := rows.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return result, fmt.Errorf("scan exact semantic cache entry: %w", err)
	}
	if exact != nil {
		result.Exact = &exact.Entry
		return result, nil
	}
	if query.MaxCandidates <= 0 {
		return result, nil
	}

	rows, err = repository.executor.QueryContext(ctx, responseSemanticCachePGVectorCandidateQuery,
		vector,
		query.Scope.WorkspaceID, query.Scope.Model, query.Scope.Provider, query.Scope.ChannelID,
		query.Scope.UpstreamModel, query.Scope.Capability, query.Scope.PromptID, query.Scope.PromptVersion,
		query.Scope.PromptChecksum, query.Scope.GuardPolicyVersion, query.Scope.DataClassification,
		query.Scope.OutputSignature, query.Scope.Region, query.Now, query.Threshold, query.MaxCandidates,
	)
	if err != nil {
		return result, fmt.Errorf("lookup semantic cache candidates: %w", err)
	}
	for rows.Next() {
		candidate, similarity, scanErr := scanPGVectorResponseSemanticCacheCandidate(rows)
		if scanErr != nil {
			_ = rows.Close()
			return result, fmt.Errorf("scan semantic cache candidate: %w", scanErr)
		}
		result.Candidates = append(result.Candidates, ResponseSemanticCacheCandidate{Key: candidate.Key, Entry: candidate.Entry, Similarity: similarity})
	}
	if err := rows.Err(); err != nil {
		_ = rows.Close()
		return result, fmt.Errorf("iterate semantic cache candidates: %w", err)
	}
	if err := rows.Close(); err != nil {
		return result, fmt.Errorf("close semantic cache candidates: %w", err)
	}
	return result, nil
}

func responseSemanticCacheExactArgs(query ResponseSemanticCacheLookupRequest) []any {
	scope := query.Scope
	return []any{
		query.Key, scope.WorkspaceID, scope.Model, scope.Provider, scope.ChannelID,
		scope.UpstreamModel, scope.Capability, scope.PromptID, scope.PromptVersion,
		scope.PromptChecksum, scope.GuardPolicyVersion, scope.DataClassification,
		scope.OutputSignature, scope.Region, query.Now,
	}
}

func (repository *pgvectorResponseSemanticCache) Put(ctx context.Context, key string, entry ResponseSemanticCacheEntry) error {
	if repository == nil || repository.executor == nil {
		return errors.New("pgvector repository is closed")
	}
	if strings.TrimSpace(key) == "" || strings.TrimSpace(entry.Text) == "" || len(entry.Text) > responseSemanticCacheMaxText {
		return errors.New("semantic cache entry has invalid text or key")
	}
	if entry.CompletionTokens < 0 || entry.CreatedAt.IsZero() || entry.ExpiresAt.IsZero() || !entry.ExpiresAt.After(entry.CreatedAt) {
		return errors.New("semantic cache entry has invalid timestamps or completion tokens")
	}
	vector, err := responseSemanticCacheVectorText(entry.Embedding)
	if err != nil {
		return err
	}
	scope := entry.Scope
	err = repository.executor.ExecContext(ctx, responseSemanticCachePGVectorPutQuery,
		key, entry.Text, entry.CompletionTokens, scope.WorkspaceID, scope.Model, scope.Provider,
		scope.ChannelID, scope.UpstreamModel, scope.Capability, scope.PromptID, scope.PromptVersion,
		scope.PromptChecksum, scope.GuardPolicyVersion, scope.DataClassification, scope.OutputSignature,
		scope.Region, vector, entry.CreatedAt, entry.ExpiresAt,
	)
	if err != nil {
		return fmt.Errorf("put semantic cache entry: %w", err)
	}
	return nil
}

func responseSemanticCacheVectorText(embedding []float64) (string, error) {
	if len(embedding) != responseSemanticCacheEmbeddingDimensions {
		return "", errors.New("semantic cache embedding has invalid dimensions")
	}
	values := make([]string, len(embedding))
	for index, value := range embedding {
		if math.IsNaN(value) || math.IsInf(value, 0) {
			return "", errors.New("semantic cache embedding contains a non-finite value")
		}
		values[index] = strconv.FormatFloat(value, 'g', -1, 64)
	}
	return "[" + strings.Join(values, ",") + "]", nil
}

func scanPGVectorResponseSemanticCacheEntry(rows ResponseSemanticCacheSQLRows, key string) (*ResponseSemanticCacheCandidate, error) {
	var entry responseSemanticCacheEntry
	var embedding string
	if !rows.Next() {
		if err := rows.Err(); err != nil {
			return nil, err
		}
		return nil, nil
	}
	if err := rows.Scan(
		&entry.Text, &entry.CompletionTokens, &entry.CreatedAt, &entry.ExpiresAt,
		&entry.Scope.WorkspaceID, &entry.Scope.Model, &entry.Scope.Provider, &entry.Scope.ChannelID,
		&entry.Scope.UpstreamModel, &entry.Scope.Capability, &entry.Scope.PromptID, &entry.Scope.PromptVersion,
		&entry.Scope.PromptChecksum, &entry.Scope.GuardPolicyVersion, &entry.Scope.DataClassification,
		&entry.Scope.OutputSignature, &entry.Scope.Region, &embedding,
	); err != nil {
		return nil, err
	}
	parsed, err := responseSemanticCacheParseVectorText(embedding)
	if err != nil {
		return nil, err
	}
	entry.Embedding = parsed
	return &ResponseSemanticCacheCandidate{Key: key, Entry: entry}, nil
}

func scanPGVectorResponseSemanticCacheCandidate(rows ResponseSemanticCacheSQLRows) (ResponseSemanticCacheCandidate, float64, error) {
	var key string
	var entry responseSemanticCacheEntry
	var embedding string
	var similarity float64
	if err := rows.Scan(
		&key, &entry.Text, &entry.CompletionTokens, &entry.CreatedAt, &entry.ExpiresAt,
		&entry.Scope.WorkspaceID, &entry.Scope.Model, &entry.Scope.Provider, &entry.Scope.ChannelID,
		&entry.Scope.UpstreamModel, &entry.Scope.Capability, &entry.Scope.PromptID, &entry.Scope.PromptVersion,
		&entry.Scope.PromptChecksum, &entry.Scope.GuardPolicyVersion, &entry.Scope.DataClassification,
		&entry.Scope.OutputSignature, &entry.Scope.Region, &embedding, &similarity,
	); err != nil {
		return ResponseSemanticCacheCandidate{}, 0, err
	}
	parsed, err := responseSemanticCacheParseVectorText(embedding)
	if err != nil {
		return ResponseSemanticCacheCandidate{}, 0, err
	}
	entry.Embedding = parsed
	return ResponseSemanticCacheCandidate{Key: key, Entry: entry}, similarity, nil
}

func responseSemanticCacheParseVectorText(value string) ([]float64, error) {
	value = strings.TrimSpace(value)
	if len(value) < 2 || value[0] != '[' || value[len(value)-1] != ']' {
		return nil, errors.New("semantic cache embedding has invalid vector syntax")
	}
	items := strings.Split(value[1:len(value)-1], ",")
	if len(items) != responseSemanticCacheEmbeddingDimensions {
		return nil, errors.New("semantic cache embedding has invalid dimensions")
	}
	result := make([]float64, len(items))
	for index, item := range items {
		parsed, err := strconv.ParseFloat(strings.TrimSpace(item), 64)
		if err != nil || math.IsNaN(parsed) || math.IsInf(parsed, 0) {
			return nil, errors.New("semantic cache embedding contains an invalid value")
		}
		result[index] = parsed
	}
	return result, nil
}

func (s *Server) Close() error {
	if s == nil {
		return nil
	}
	store := &s.responses
	store.ensure(s.Logger)
	store.mu.Lock()
	repository := store.semanticCacheRepository
	store.semanticCacheRepository = responseSemanticCacheDisabledRepository
	store.mu.Unlock()
	if closer, ok := repository.(interface{ Close() error }); ok {
		return closer.Close()
	}
	return nil
}

func (s *Server) SetResponseSemanticCacheRepository(repository ResponseSemanticCacheRepository) {
	if s == nil {
		return
	}
	store := &s.responses
	store.ensure(s.Logger)
	if repository == nil {
		repository = responseSemanticCacheDisabledRepository
	}
	store.mu.Lock()
	previous := store.semanticCacheRepository
	store.semanticCacheRepository = repository
	store.mu.Unlock()
	if previous != nil {
		if closer, ok := previous.(interface{ Close() error }); ok {
			_ = closer.Close()
		}
	}
}

var _ ResponseSemanticCacheRepository = (*pgvectorResponseSemanticCache)(nil)
