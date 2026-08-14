-- WorkAMA workflow code nodes use a run-owned sandbox scope, not an agent session.
ALTER TABLE ag_sandbox ALTER COLUMN session_id DROP NOT NULL;
ALTER TABLE ag_sandbox DROP CONSTRAINT IF EXISTS ag_sandbox_session_id_fkey;
ALTER TABLE ag_sandbox ADD COLUMN IF NOT EXISTS scope_type TEXT NOT NULL DEFAULT 'session';
ALTER TABLE ag_sandbox ADD COLUMN IF NOT EXISTS scope_id TEXT;
UPDATE ag_sandbox SET scope_id = session_id WHERE scope_id IS NULL;
ALTER TABLE ag_sandbox ALTER COLUMN scope_id SET NOT NULL;
ALTER TABLE ag_sandbox DROP CONSTRAINT IF EXISTS ag_sandbox_scope_type_check;
ALTER TABLE ag_sandbox ADD CONSTRAINT ag_sandbox_scope_type_check CHECK (scope_type IN ('session','workflow'));
CREATE UNIQUE INDEX IF NOT EXISTS uq_ag_sandbox_active_scope
  ON ag_sandbox(scope_type,scope_id) WHERE status IN ('active','sleeping');
