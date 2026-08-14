ALTER TABLE ag_sandbox ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'docker';
ALTER TABLE ag_sandbox ADD COLUMN IF NOT EXISTS runtime_contract TEXT NOT NULL DEFAULT 'docker-runtime';
CREATE INDEX IF NOT EXISTS ix_ag_sandbox_provider_status ON ag_sandbox(provider, status);
