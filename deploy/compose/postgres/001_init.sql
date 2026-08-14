CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS id_user (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    email_verified BOOLEAN NOT NULL DEFAULT FALSE,
    failed_login_count INTEGER NOT NULL DEFAULT 0,
    locked_until TIMESTAMPTZ,
    onboarding_completed BOOLEAN NOT NULL DEFAULT FALSE,
    profile JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS id_org (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner_user_id TEXT NOT NULL REFERENCES id_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS id_workspace (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES id_org(id),
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    settings JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(org_id, slug)
);

CREATE TABLE IF NOT EXISTS id_member (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES id_org(id),
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
    user_id TEXT NOT NULL REFERENCES id_user(id),
    role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, user_id)
);

CREATE TABLE IF NOT EXISTS id_refresh_token (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES id_user(id),
    workspace_id TEXT,
    token_hash TEXT NOT NULL UNIQUE,
    family_id TEXT NOT NULL,
    parent_id TEXT REFERENCES id_refresh_token(id),
    rotated_to_id TEXT REFERENCES id_refresh_token(id),
    expires_at TIMESTAMPTZ NOT NULL,
    last_used_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_id_refresh_token_family ON id_refresh_token(family_id);

CREATE TABLE IF NOT EXISTS id_auth_token (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
    token_type TEXT NOT NULL CHECK (token_type IN ('email_verify', 'password_reset')),
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_id_auth_token_user_type ON id_auth_token(user_id, token_type, created_at DESC);

CREATE TABLE IF NOT EXISTS id_mfa_factor (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
    factor_type TEXT NOT NULL DEFAULT 'totp' CHECK (factor_type IN ('totp')),
    secret_enc TEXT NOT NULL,
    confirmed_at TIMESTAMPTZ,
    disabled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id, factor_type)
);

CREATE TABLE IF NOT EXISTS id_api_key (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
    actor_user_id TEXT NOT NULL REFERENCES id_user(id),
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    last_four TEXT NOT NULL,
    scopes TEXT[] NOT NULL DEFAULT ARRAY['platform:read']::text[],
    resource_allowlist TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
    expires_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS id_consent (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES id_user(id),
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
    policy_type TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    accepted BOOLEAN NOT NULL,
    locale TEXT NOT NULL DEFAULT 'zh-CN',
    display_text_hash TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'web',
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    withdrawn_at TIMESTAMPTZ,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id, workspace_id, policy_type)
);

CREATE TABLE IF NOT EXISTS id_data_request (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES id_user(id),
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
    request_type TEXT NOT NULL CHECK (request_type IN ('access', 'export', 'delete', 'correct')),
    scope TEXT NOT NULL DEFAULT 'content',
    status TEXT NOT NULL DEFAULT 'requested' CHECK (status IN ('requested','identity_verification','scoped','approved','rejected','executing','verification','completed','partially_completed')),
    identity_verified_at TIMESTAMPTZ,
    result_manifest JSONB,
    result_checksum TEXT,
    exceptions JSONB NOT NULL DEFAULT '[]'::jsonb,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS id_data_request_step (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES id_data_request(id) ON DELETE CASCADE,
    step_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed','skipped')),
    resource_count INTEGER NOT NULL DEFAULT 0,
    action TEXT,
    checksum TEXT,
    error TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    UNIQUE(request_id, step_name)
);

CREATE TABLE IF NOT EXISTS id_deletion_tombstone (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES id_data_request(id),
    user_id TEXT NOT NULL REFERENCES id_user(id),
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
    scope TEXT NOT NULL,
    replay_version INTEGER NOT NULL DEFAULT 1,
    resource_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(request_id, scope)
);

CREATE TABLE IF NOT EXISTS ops_feature_flag (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id),
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
  flag_key TEXT NOT NULL,
  version INTEGER NOT NULL,
  flag_type TEXT NOT NULL CHECK (flag_type IN ('release','experiment','ops','entitlement','compliance')),
  default_value JSONB NOT NULL DEFAULT 'false'::jsonb,
  safe_value JSONB NOT NULL DEFAULT 'false'::jsonb,
  targeting JSONB NOT NULL DEFAULT '{}'::jsonb,
  salt TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'disabled' CHECK (status IN ('draft','enabled','disabled','archived')),
  owner TEXT NOT NULL,
  runbook TEXT,
  metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
  starts_at TIMESTAMPTZ,
  ends_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ,
  previous_version INTEGER,
  content_hash TEXT NOT NULL,
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id, flag_key, version)
);

CREATE TABLE IF NOT EXISTS ops_dynamic_config (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id),
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
  config_key TEXT NOT NULL,
  version INTEGER NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1,
  value_schema JSONB NOT NULL,
  config_value JSONB NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','enabled','disabled','archived')),
  risk_level TEXT NOT NULL DEFAULT 'normal' CHECK (risk_level IN ('normal','high')),
  effective_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ,
  approved_by TEXT REFERENCES id_user(id),
  previous_version INTEGER,
  content_hash TEXT NOT NULL,
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id, config_key, version)
);

CREATE TABLE IF NOT EXISTS ops_event_catalog (
  event_name TEXT PRIMARY KEY,
  domain TEXT NOT NULL,
  event_version INTEGER NOT NULL DEFAULT 1,
  allowed_properties JSONB NOT NULL DEFAULT '[]'::jsonb,
  required_properties JSONB NOT NULL DEFAULT '[]'::jsonb,
  retention_days INTEGER NOT NULL DEFAULT 395,
  owner TEXT NOT NULL DEFAULT 'product-ops',
  status TEXT NOT NULL DEFAULT 'active',
  content_hash TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ops_product_event (
  id TEXT PRIMARY KEY,
  event_name TEXT NOT NULL REFERENCES ops_event_catalog(event_name),
  event_version INTEGER NOT NULL DEFAULT 1,
  user_id TEXT REFERENCES id_user(id),
  org_id TEXT REFERENCES id_org(id),
  workspace_id TEXT REFERENCES id_workspace(id),
  client TEXT NOT NULL DEFAULT 'web',
  client_version TEXT,
  locale TEXT,
  region TEXT,
  session_ref TEXT,
  experiment_assignments JSONB NOT NULL DEFAULT '{}'::jsonb,
  properties JSONB NOT NULL DEFAULT '{}'::jsonb,
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ops_product_event_workspace_time
  ON ops_product_event(workspace_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS ops_release_evidence (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
  release_version TEXT NOT NULL,
  environment TEXT NOT NULL CHECK (environment IN ('dev','ci','staging','preprod','prod')),
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','verified','approved','released','rolled_back')),
  commit_ref TEXT,
  image_refs JSONB NOT NULL DEFAULT '{}'::jsonb,
  test_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  migration_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  security_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  rollback_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  approvals JSONB NOT NULL DEFAULT '[]'::jsonb,
  content_hash TEXT NOT NULL,
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id, release_version, environment)
);

CREATE TABLE IF NOT EXISTS ops_outbox (
  id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  workspace_id TEXT,
  trace_id TEXT,
  payload JSONB NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','published','failed')),
  attempts INTEGER NOT NULL DEFAULT 0,
  available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  published_at TIMESTAMPTZ,
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ops_outbox_pending ON ops_outbox(status, available_at, created_at);

-- Shared async operation/job runtime used by migrations and all services.
-- Keep this additive base schema in the init migration so a fresh Compose
-- volume can run numbered migrations before platform-api starts.
CREATE TABLE IF NOT EXISTS ops_async_operation (
  id TEXT PRIMARY KEY, operation_type TEXT NOT NULL, schema_version INTEGER NOT NULL DEFAULT 1,
  org_id TEXT NOT NULL REFERENCES id_org(id), workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
  actor_id TEXT NOT NULL REFERENCES id_user(id), actor_role TEXT NOT NULL,
  idempotency_key TEXT NOT NULL, input_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
  progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100), stage TEXT,
  cancellable BOOLEAN NOT NULL DEFAULT TRUE, attempt_count INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3, result_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  error_code TEXT, error_message TEXT, cancellation_reason TEXT, trace_id TEXT,
  policy_version TEXT, cancel_requested_at TIMESTAMPTZ, started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ, expires_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(workspace_id, operation_type, idempotency_key)
);

CREATE TABLE IF NOT EXISTS ops_job (
  id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES ops_async_operation(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id), job_type TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1, queue TEXT NOT NULL DEFAULT 'platform',
  priority INTEGER NOT NULL DEFAULT 100, payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  payload_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', attempt_count INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3, timeout_seconds INTEGER NOT NULL DEFAULT 300,
  heartbeat_seconds INTEGER NOT NULL DEFAULT 15, cancellable BOOLEAN NOT NULL DEFAULT TRUE,
  progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100), stage TEXT,
  lease_owner TEXT, lease_token TEXT, lease_expires_at TIMESTAMPTZ, heartbeat_at TIMESTAMPTZ,
  scheduled_at TIMESTAMPTZ NOT NULL DEFAULT now(), cancel_requested_at TIMESTAMPTZ,
  cancellation_reason TEXT, last_error_code TEXT, last_error TEXT, started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ops_job_claim ON ops_job(status, scheduled_at, priority DESC);
CREATE INDEX IF NOT EXISTS idx_ops_job_queue_claim ON ops_job(queue, status, scheduled_at, priority DESC);

CREATE TABLE IF NOT EXISTS ops_job_run (
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES ops_job(id) ON DELETE CASCADE,
  attempt INTEGER NOT NULL, worker_id TEXT NOT NULL, status TEXT NOT NULL,
  started_at TIMESTAMPTZ NOT NULL, heartbeat_at TIMESTAMPTZ, ended_at TIMESTAMPTZ,
  result_summary JSONB NOT NULL DEFAULT '{}'::jsonb, error_code TEXT, error_summary TEXT,
  metrics JSONB NOT NULL DEFAULT '{}'::jsonb, UNIQUE(job_id, attempt)
);

CREATE TABLE IF NOT EXISTS ops_job_dlq (
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES ops_job(id),
  operation_id TEXT NOT NULL REFERENCES ops_async_operation(id), workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
  job_type TEXT NOT NULL, payload_hash TEXT NOT NULL, attempts INTEGER NOT NULL,
  error_code TEXT, error_summary TEXT, failed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  replayed_at TIMESTAMPTZ, replayed_by TEXT REFERENCES id_user(id), replay_reason TEXT, replay_job_id TEXT
);

CREATE TABLE IF NOT EXISTS id_processing_activity (
    table_name TEXT PRIMARY KEY,
    classification TEXT NOT NULL CHECK (classification IN ('C0','C1','C2','C3','C4')),
    purpose TEXT NOT NULL,
    owner TEXT NOT NULL,
    region TEXT NOT NULL,
    retention_days INTEGER NOT NULL,
    deletion_behavior TEXT NOT NULL,
    reviewed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS id_notification (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES id_user(id),
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
    event_type TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal',
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    action_url TEXT,
    payload_min JSONB NOT NULL DEFAULT '{}'::jsonb,
    resource_ref TEXT,
    dedupe_key TEXT NOT NULL,
    read_at TIMESTAMPTZ,
    archived_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id, dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_id_notification_user_time
    ON id_notification(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS id_notification_delivery (
    id TEXT PRIMARY KEY,
    notification_id TEXT NOT NULL REFERENCES id_notification(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    provider TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed')),
    provider_id TEXT,
    error_class TEXT,
    next_attempt_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(notification_id, channel)
);

CREATE TABLE IF NOT EXISTS sec_policy (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL UNIQUE REFERENCES id_workspace(id) ON DELETE CASCADE,
    input_action TEXT NOT NULL DEFAULT 'log' CHECK (input_action IN ('block', 'mask', 'log')),
    output_action TEXT NOT NULL DEFAULT 'block' CHECK (output_action IN ('block', 'mask', 'log')),
    blocked_terms TEXT[] NOT NULL DEFAULT ARRAY['api_key','system prompt','身份证号']::text[],
    autonomy_level TEXT NOT NULL DEFAULT 'A2' CHECK (autonomy_level IN ('A1', 'A2', 'A3', 'A4')),
    domain_allowlist TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
    domain_denylist TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
    updated_by TEXT REFERENCES id_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sec_moderation_log (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    direction TEXT NOT NULL CHECK (direction IN ('input', 'output')),
    action TEXT NOT NULL CHECK (action IN ('block', 'mask', 'log')),
    matched_terms TEXT[] NOT NULL,
    content_hash TEXT NOT NULL,
    request_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sec_moderation_log_workspace_time ON sec_moderation_log(workspace_id, created_at DESC);

CREATE TABLE IF NOT EXISTS sec_prompt_version (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    checksum TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published', 'archived')),
    created_by TEXT NOT NULL REFERENCES id_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ,
    UNIQUE(workspace_id, name, version)
);

CREATE TABLE IF NOT EXISTS sec_eval_run (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    prompt_version_id TEXT NOT NULL REFERENCES sec_prompt_version(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('passed', 'failed')),
    total_cases INTEGER NOT NULL,
    passed_cases INTEGER NOT NULL,
    failures JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS gw_channel (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
    name TEXT NOT NULL,
    provider TEXT NOT NULL,
    base_url TEXT NOT NULL,
    credential_enc TEXT,
    models TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
    weight INTEGER NOT NULL DEFAULT 100,
    status TEXT NOT NULL DEFAULT 'enabled',
    last_health TEXT NOT NULL DEFAULT 'unknown',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gw_channel_workspace ON gw_channel(workspace_id);

CREATE TABLE IF NOT EXISTS gw_model_mapping (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
    model TEXT NOT NULL,
    channel_id TEXT NOT NULL REFERENCES gw_channel(id) ON DELETE CASCADE,
    upstream_model TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, model, channel_id)
);

CREATE INDEX IF NOT EXISTS idx_gw_model_mapping_workspace_model
    ON gw_model_mapping(workspace_id, model);

CREATE TABLE IF NOT EXISTS gw_token_group (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
    name TEXT NOT NULL,
    rpm_limit INTEGER NOT NULL DEFAULT 600,
    tpm_limit INTEGER NOT NULL DEFAULT 1000000,
    model_whitelist TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
    pinned_channel_id TEXT,
    fallback_chain JSONB NOT NULL DEFAULT '{}'::jsonb,
    model_mapping_override JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'enabled',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, name)
);

CREATE TABLE IF NOT EXISTS gw_token (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    last_four TEXT NOT NULL,
    rpm_limit INTEGER NOT NULL DEFAULT 60,
    tpm_limit INTEGER NOT NULL DEFAULT 100000,
    model_whitelist TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
    pinned_channel_id TEXT,
    group_id TEXT REFERENCES gw_token_group(id),
    expires_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS gw_model_price (
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
    model TEXT NOT NULL,
    input_per_million NUMERIC(18,6) NOT NULL DEFAULT 1,
    output_per_million NUMERIC(18,6) NOT NULL DEFAULT 2,
    markup_percent NUMERIC(8,2) NOT NULL DEFAULT 10,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(workspace_id, model)
);

CREATE TABLE IF NOT EXISTS gw_request_log (
    request_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
    token_id TEXT,
    channel_id TEXT,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cost_credits NUMERIC(18,6) NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    status_code INTEGER NOT NULL,
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gw_request_log_workspace_time ON gw_request_log(workspace_id, created_at DESC);

CREATE TABLE IF NOT EXISTS bill_account (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL UNIQUE REFERENCES id_workspace(id),
    granted_balance NUMERIC(18,6) NOT NULL DEFAULT 500,
    purchased_balance NUMERIC(18,6) NOT NULL DEFAULT 0,
    frozen_balance NUMERIC(18,6) NOT NULL DEFAULT 0,
    version BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bill_reservation (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
    request_id TEXT NOT NULL UNIQUE,
    model TEXT NOT NULL,
    estimated_cost NUMERIC(18,6) NOT NULL,
    actual_cost NUMERIC(18,6),
    status TEXT NOT NULL CHECK (status IN ('frozen', 'settled', 'released')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_bill_reservation_workspace_status
    ON bill_reservation(workspace_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS bill_transaction (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
    kind TEXT NOT NULL,
    amount NUMERIC(18,6) NOT NULL,
    balance_after NUMERIC(18,6) NOT NULL,
    reference_id TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, reference_id, kind)
);

CREATE TABLE IF NOT EXISTS bill_usage_record (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
    request_id TEXT NOT NULL UNIQUE,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    cost_credits NUMERIC(18,6) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bill_usage_hourly (
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
    resource TEXT NOT NULL DEFAULT 'llm',
    model TEXT NOT NULL,
    hour TIMESTAMPTZ NOT NULL,
    requests BIGINT NOT NULL DEFAULT 0,
    prompt_tokens BIGINT NOT NULL DEFAULT 0,
    completion_tokens BIGINT NOT NULL DEFAULT 0,
    cost_credits NUMERIC(18,6) NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(workspace_id, resource, model, hour)
);

CREATE TABLE IF NOT EXISTS bill_reconciliation_run (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
    business_date DATE NOT NULL,
    usage_credits NUMERIC(18,6) NOT NULL,
    ledger_credits NUMERIC(18,6) NOT NULL,
    difference NUMERIC(18,6) NOT NULL,
    difference_ratio NUMERIC(18,6) NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('passed', 'mismatch')),
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, business_date)
);

CREATE TABLE IF NOT EXISTS ops_inbox (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    consumer_name TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'processing',
    last_error TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ,
    request_id TEXT,
    UNIQUE(event_id, consumer_name)
);

CREATE INDEX IF NOT EXISTS idx_ops_inbox_consumer_status
    ON ops_inbox(consumer_name, status, received_at);

CREATE INDEX IF NOT EXISTS idx_ops_inbox_request_id
    ON ops_inbox(request_id);

CREATE TABLE IF NOT EXISTS ag_session (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
    user_id TEXT NOT NULL REFERENCES id_user(id),
    title TEXT NOT NULL DEFAULT 'New conversation',
    model TEXT NOT NULL DEFAULT 'workama-chat',
    status TEXT NOT NULL DEFAULT 'idle',
    last_seq BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ag_session_workspace_time ON ag_session(workspace_id, updated_at DESC);

ALTER TABLE ag_session ADD COLUMN IF NOT EXISTS agent_kind TEXT NOT NULL DEFAULT 'ama_chat';
ALTER TABLE ag_session ADD COLUMN IF NOT EXISTS model_config JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE ag_session ADD COLUMN IF NOT EXISTS toolset TEXT[] NOT NULL DEFAULT ARRAY['web_search','file.read','file.write','file.search','code_interpreter','terminal']::text[];
ALTER TABLE ag_session ALTER COLUMN toolset SET DEFAULT ARRAY['web_search','file.read','file.write','file.search','code_interpreter','terminal']::text[];
ALTER TABLE ag_session ADD COLUMN IF NOT EXISTS canvas_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE ag_session ADD COLUMN IF NOT EXISTS prompt_version_id TEXT REFERENCES sec_prompt_version(id) ON DELETE SET NULL;
ALTER TABLE ag_session ADD COLUMN IF NOT EXISTS max_steps INTEGER NOT NULL DEFAULT 50;
ALTER TABLE ag_session ADD COLUMN IF NOT EXISTS max_credits NUMERIC(18,6) NOT NULL DEFAULT 500;
ALTER TABLE ag_session ADD COLUMN IF NOT EXISTS max_duration_seconds INTEGER NOT NULL DEFAULT 3600;
ALTER TABLE ag_session ADD COLUMN IF NOT EXISTS used_steps INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ag_session ADD COLUMN IF NOT EXISTS used_credits NUMERIC(18,6) NOT NULL DEFAULT 0;
ALTER TABLE ag_session ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS ag_event (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES ag_session(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
    seq BIGINT NOT NULL,
    type TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(session_id, seq)
);

CREATE TABLE IF NOT EXISTS ag_attachment (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES ag_session(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    extracted_text TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ready',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE ag_attachment ADD COLUMN IF NOT EXISTS s3_key TEXT;
ALTER TABLE ag_attachment ADD COLUMN IF NOT EXISTS content_sha256 TEXT;
ALTER TABLE ag_attachment ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '24 hours');
ALTER TABLE ag_attachment ADD COLUMN IF NOT EXISTS parse_error TEXT;

CREATE TABLE IF NOT EXISTS ag_attachment_upload (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES ag_session(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    expected_size BIGINT NOT NULL CHECK (expected_size >= 0 AND expected_size <= 5242880),
    expected_sha256 TEXT,
    s3_key TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'prepared' CHECK (status IN ('prepared','uploaded','completed','expired')),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '15 minutes'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ag_artifact (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES ag_session(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
    name TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'text/markdown',
    content TEXT NOT NULL,
    share_token TEXT UNIQUE,
    share_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE ag_artifact ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'file';
ALTER TABLE ag_artifact ADD COLUMN IF NOT EXISTS s3_key TEXT;
ALTER TABLE ag_artifact ADD COLUMN IF NOT EXISTS size_bytes BIGINT NOT NULL DEFAULT 0;
ALTER TABLE ag_artifact ADD COLUMN IF NOT EXISTS content_sha256 TEXT;
ALTER TABLE ag_artifact ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'ready';
ALTER TABLE ag_artifact ADD COLUMN IF NOT EXISTS preview JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE ag_artifact ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE ag_artifact ADD COLUMN IF NOT EXISTS provenance_status TEXT NOT NULL DEFAULT 'not_applicable';
ALTER TABLE ag_artifact ADD COLUMN IF NOT EXISTS provenance_hash TEXT;
ALTER TABLE ag_artifact ADD COLUMN IF NOT EXISTS parent_artifact_ids TEXT[] NOT NULL DEFAULT ARRAY[]::text[];
ALTER TABLE ag_artifact ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE ag_artifact ADD COLUMN IF NOT EXISTS purge_after TIMESTAMPTZ;
ALTER TABLE ag_artifact ADD COLUMN IF NOT EXISTS delete_reason TEXT;

CREATE TABLE IF NOT EXISTS ag_artifact_share (
    id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL REFERENCES ag_artifact(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE, expires_at TIMESTAMPTZ NOT NULL,
    max_downloads INTEGER, download_count INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL REFERENCES id_user(id), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ, revoke_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_ag_artifact_share_artifact ON ag_artifact_share(artifact_id,created_at DESC);

CREATE TABLE IF NOT EXISTS ag_sandbox (
    id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES ag_session(id) ON DELETE CASCADE,
    scope_type TEXT NOT NULL DEFAULT 'session',
    scope_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
    runtime TEXT NOT NULL,
    image TEXT NOT NULL,
    container_id TEXT NOT NULL,
    volume_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active','sleeping','released','failed')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ,
    meter_seconds BIGINT NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_ag_sandbox_workspace_status ON ag_sandbox(workspace_id,status,last_active_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ag_sandbox_active_session ON ag_sandbox(session_id) WHERE status IN ('active','sleeping');
CREATE UNIQUE INDEX IF NOT EXISTS uq_ag_sandbox_active_scope ON ag_sandbox(scope_type,scope_id) WHERE status IN ('active','sleeping');
ALTER TABLE ag_sandbox ADD COLUMN IF NOT EXISTS snapshot_s3_key TEXT;
ALTER TABLE ag_sandbox ADD COLUMN IF NOT EXISTS snapshot_sha256 TEXT;
ALTER TABLE ag_sandbox ADD COLUMN IF NOT EXISTS snapshot_size_bytes BIGINT;
ALTER TABLE ag_sandbox ADD COLUMN IF NOT EXISTS snapshotted_at TIMESTAMPTZ;
ALTER TABLE ag_sandbox ADD COLUMN IF NOT EXISTS restore_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ag_sandbox ADD COLUMN IF NOT EXISTS allocation_source TEXT NOT NULL DEFAULT 'cold';
ALTER TABLE ag_sandbox ADD COLUMN IF NOT EXISTS cold_start_ms INTEGER;

CREATE TABLE IF NOT EXISTS ag_approval (
    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES ag_session(id) ON DELETE CASCADE, call_id TEXT NOT NULL,
    requester_id TEXT NOT NULL REFERENCES id_user(id), tool_name TEXT NOT NULL, action_hash TEXT NOT NULL,
    risk TEXT NOT NULL CHECK (risk IN ('A3','A4')), preview JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','expired','consumed')),
    reason TEXT, decided_by TEXT REFERENCES id_user(id), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL, decided_at TIMESTAMPTZ, consumed_at TIMESTAMPTZ,
    UNIQUE(workspace_id, call_id)
);
CREATE INDEX IF NOT EXISTS idx_ag_approval_workspace_status ON ag_approval(workspace_id,status,created_at DESC);

CREATE TABLE IF NOT EXISTS ag_tool_grant (
    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    tool_name TEXT NOT NULL, scope TEXT NOT NULL CHECK (scope IN ('workspace','session')),
    session_id TEXT REFERENCES ag_session(id) ON DELETE CASCADE, max_risk TEXT NOT NULL CHECK (max_risk IN ('A1','A2')),
    created_by TEXT NOT NULL REFERENCES id_user(id), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ, revoked_at TIMESTAMPTZ, revoke_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_ag_tool_grant_workspace_active ON ag_tool_grant(workspace_id,tool_name,expires_at) WHERE revoked_at IS NULL;
