ALTER TABLE id_federation_sso_config
  ADD COLUMN IF NOT EXISTS certificate_enc TEXT;
