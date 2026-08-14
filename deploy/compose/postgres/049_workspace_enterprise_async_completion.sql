-- v7.125 P1 workspace/enterprise async completion
-- Adds the id_member relaxations required by the organization owner-transfer
-- and deletion-retention flows.  An org-level member (workspace_id IS NULL) can
-- now be the proposed new owner, and the unique constraint is moved from the
-- workspace-only pair to the (org, workspace, user) tuple so both workspace
-- memberships and org-only memberships coexist safely.
--
-- Statements are idempotent; they are also applied at runtime by
-- ensure_workspaces_schema / ensure_enterprise_schema for existing volumes.

ALTER TABLE id_member ALTER COLUMN workspace_id DROP NOT NULL;
ALTER TABLE id_member DROP CONSTRAINT IF EXISTS id_member_workspace_id_user_id_key;
CREATE UNIQUE INDEX IF NOT EXISTS idx_id_member_org_workspace_user
    ON id_member(org_id, workspace_id, user_id);

-- The async owner-transfer and organization-deletion workers update
-- id_workspace timestamps; make sure the column exists on fresh databases.
ALTER TABLE id_workspace ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
