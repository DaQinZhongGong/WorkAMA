-- OIDC authorization-code exchange fields. Secrets remain encrypted/hash-only in API responses.
ALTER TABLE id_federation_sso_config
    ADD COLUMN IF NOT EXISTS token_endpoint TEXT,
    ADD COLUMN IF NOT EXISTS jwks_uri TEXT,
    ADD COLUMN IF NOT EXISTS client_secret_enc TEXT;

ALTER TABLE id_federation_oidc_state
    ADD COLUMN IF NOT EXISTS code_verifier_enc TEXT;
