package server

import (
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
	"time"
)

type persistedResponseRecord struct {
	WorkspaceID  string         `json:"workspace_id"`
	Model        string         `json:"model"`
	RequestID    string         `json:"request_id"`
	Background   bool           `json:"background"`
	ExpiresAt    time.Time      `json:"expires_at"`
	Object       responseObject `json:"object"`
	ChatBody     ChatRequest    `json:"chat_body,omitempty"`
	PromptTokens int            `json:"prompt_tokens,omitempty"`
}

type responsePersistence interface {
	Load() (map[string]persistedResponseRecord, error)
	Save(string, persistedResponseRecord) error
	Delete(string) error
}

type memoryResponsePersistence struct {
	mu      sync.Mutex
	records map[string]persistedResponseRecord
}

func newMemoryResponsePersistence() responsePersistence {
	return &memoryResponsePersistence{records: make(map[string]persistedResponseRecord)}
}

func (p *memoryResponsePersistence) Load() (map[string]persistedResponseRecord, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	copyValue := make(map[string]persistedResponseRecord, len(p.records))
	for key, value := range p.records {
		copyValue[key] = value
	}
	return copyValue, nil
}

func (p *memoryResponsePersistence) Save(key string, value persistedResponseRecord) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.records[key] = value
	return nil
}

func (p *memoryResponsePersistence) Delete(key string) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	delete(p.records, key)
	return nil
}

type fileResponsePersistence struct {
	mu   sync.Mutex
	path string
}

func newFileResponsePersistence(path string) responsePersistence {
	return &fileResponsePersistence{path: path}
}

func (p *fileResponsePersistence) Load() (map[string]persistedResponseRecord, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	data, err := os.ReadFile(p.path)
	if errors.Is(err, os.ErrNotExist) {
		return map[string]persistedResponseRecord{}, nil
	}
	if err != nil {
		return nil, err
	}
	var records map[string]persistedResponseRecord
	if err := json.Unmarshal(data, &records); err != nil {
		return nil, fmt.Errorf("decode response persistence: %w", err)
	}
	if records == nil {
		records = map[string]persistedResponseRecord{}
	}
	return records, nil
}

func (p *fileResponsePersistence) Save(key string, value persistedResponseRecord) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	records, err := p.loadLocked()
	if err != nil {
		return err
	}
	records[key] = value
	return p.writeLocked(records)
}

func (p *fileResponsePersistence) Delete(key string) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	records, err := p.loadLocked()
	if err != nil {
		return err
	}
	delete(records, key)
	return p.writeLocked(records)
}

func (p *fileResponsePersistence) loadLocked() (map[string]persistedResponseRecord, error) {
	data, err := os.ReadFile(p.path)
	if errors.Is(err, os.ErrNotExist) {
		return map[string]persistedResponseRecord{}, nil
	}
	if err != nil {
		return nil, err
	}
	var records map[string]persistedResponseRecord
	if err := json.Unmarshal(data, &records); err != nil {
		return nil, fmt.Errorf("decode response persistence: %w", err)
	}
	if records == nil {
		records = map[string]persistedResponseRecord{}
	}
	return records, nil
}

func (p *fileResponsePersistence) writeLocked(records map[string]persistedResponseRecord) error {
	if err := os.MkdirAll(filepath.Dir(p.path), 0o750); err != nil {
		return err
	}
	tmp := p.path + ".tmp"
	data, err := json.Marshal(records)
	if err != nil {
		return err
	}
	if err := os.WriteFile(tmp, data, 0o640); err != nil {
		return err
	}
	return os.Rename(tmp, p.path)
}

func newResponsePersistenceFromEnv(_ *slog.Logger) responsePersistence {
	path := os.Getenv("WORKAMA_RESPONSES_STORE")
	if path == "" {
		return newMemoryResponsePersistence()
	}
	return newFileResponsePersistence(path)
}

func responseRegistryTTLFromEnv() time.Duration {
	const fallback = 24 * time.Hour
	value := os.Getenv("WORKAMA_RESPONSES_TTL")
	if value == "" {
		return fallback
	}
	ttl, err := time.ParseDuration(value)
	if err != nil || ttl < time.Minute || ttl > 30*24*time.Hour {
		return fallback
	}
	return ttl
}

func newResponseRegistry(persistence responsePersistence, ttl time.Duration, logger *slog.Logger) responseRegistry {
	if persistence == nil {
		persistence = newMemoryResponsePersistence()
	}
	if ttl <= 0 {
		ttl = 24 * time.Hour
	}
	return responseRegistry{
		records:             make(map[string]*responseRecord),
		semanticCache:       make(map[string]responseSemanticCacheEntry),
		persistence:         persistence,
		fallbackPersistence: newMemoryResponsePersistence(),
		ttl:                 ttl,
		now:                 time.Now,
		logger:              logger,
	}
}

func (store *responseRegistry) ensure(logger *slog.Logger) {
	store.ensureOnce.Do(func() {
		if store.records == nil {
			store.records = make(map[string]*responseRecord)
		}
		if store.semanticCache == nil {
			store.semanticCache = make(map[string]responseSemanticCacheEntry)
		}
		if store.now == nil {
			store.now = time.Now
		}
		if store.ttl <= 0 {
			store.ttl = 24 * time.Hour
		}
		if store.persistence == nil {
			store.persistence = newMemoryResponsePersistence()
		}
		if store.fallbackPersistence == nil {
			store.fallbackPersistence = newMemoryResponsePersistence()
		}
		loaded, err := store.persistence.Load()
		if err != nil {
			if logger != nil {
				logger.Warn("response persistence load failed; using in-memory fallback", "error", err)
			}
			loaded, _ = store.fallbackPersistence.Load()
		}
		now := store.now()
		for id, value := range loaded {
			if !value.ExpiresAt.IsZero() && !value.ExpiresAt.After(now) {
				_ = store.persistence.Delete(id)
				continue
			}
			// Recovery: in_progress background tasks are reset to queued so they
			// can be re-scheduled by recoverBackgroundResponses.
			status := value.Object.Status
			if value.Background && status == "in_progress" {
				value.Object.Status = "queued"
				value.Object.IncompleteDetails = &responseIncompleteDetail{Reason: "recovered_after_restart"}
			}
			store.records[id] = &responseRecord{
				workspaceID:  value.WorkspaceID,
				model:        value.Model,
				requestID:    value.RequestID,
				background:   value.Background,
				expiresAt:    value.ExpiresAt,
				object:       value.Object,
				chatBody:     value.ChatBody,
				promptTokens: value.PromptTokens,
				cancel:       func() {},
			}
		}
	})
}

func (store *responseRegistry) persist(record *responseRecord) {
	if record == nil {
		return
	}
	store.mu.RLock()
	value := persistedResponseRecord{
		WorkspaceID:  record.workspaceID,
		Model:        record.model,
		RequestID:    record.requestID,
		Background:   record.background,
		ExpiresAt:    record.expiresAt,
		Object:       cloneResponseObject(record.object),
		ChatBody:     record.chatBody,
		PromptTokens: record.promptTokens,
	}
	store.mu.RUnlock()
	if store.persistence != nil {
		if err := store.persistence.Save(record.object.ID, value); err == nil {
			return
		} else if store.logger != nil {
			store.persistenceWarnOnce.Do(func() { store.logger.Warn("response persistence save failed; using in-memory fallback", "error", err) })
		}
	}
	if store.fallbackPersistence != nil {
		_ = store.fallbackPersistence.Save(record.object.ID, value)
	}
}

func (store *responseRegistry) remove(responseID string) {
	if store.persistence != nil {
		_ = store.persistence.Delete(responseID)
	}
	if store.fallbackPersistence != nil {
		_ = store.fallbackPersistence.Delete(responseID)
	}
}
