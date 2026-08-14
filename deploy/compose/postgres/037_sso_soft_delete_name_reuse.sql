ALTER TABLE id_federation_sso_config
  DROP CONSTRAINT IF EXISTS id_federation_sso_config_workspace_id_name_key;

CREATE UNIQUE INDEX IF NOT EXISTS idx_id_federation_sso_workspace_name_active
  ON id_federation_sso_config(workspace_id, name)
  WHERE status <> 'deleted';
