ALTER TABLE sec_prompt_version
    ADD COLUMN IF NOT EXISTS rollout_percent INTEGER NOT NULL DEFAULT 0;

ALTER TABLE sec_prompt_version
    DROP CONSTRAINT IF EXISTS sec_prompt_version_rollout_percent_check;

ALTER TABLE sec_prompt_version
    ADD CONSTRAINT sec_prompt_version_rollout_percent_check
    CHECK (rollout_percent BETWEEN 0 AND 100);

UPDATE sec_prompt_version
SET rollout_percent = 100
WHERE status = 'published' AND rollout_percent = 0;

WITH ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY workspace_id, name
               ORDER BY version DESC
           ) AS rank
    FROM sec_prompt_version
    WHERE status = 'published'
)
UPDATE sec_prompt_version AS prompt
SET status = 'archived', rollout_percent = 0
FROM ranked
WHERE prompt.id = ranked.id AND ranked.rank > 1;

CREATE INDEX IF NOT EXISTS idx_sec_prompt_version_rollout
    ON sec_prompt_version(workspace_id, name, status, rollout_percent, version DESC);
