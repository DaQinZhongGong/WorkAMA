-- v7.162: 技能包签名验证 + Agent 挂载执行
-- 为 skill_package 增加签名相关字段
-- 为 skill_install 增加 enabled 字段

ALTER TABLE skill_package
    ADD COLUMN IF NOT EXISTS signature TEXT,
    ADD COLUMN IF NOT EXISTS public_key TEXT,
    ADD COLUMN IF NOT EXISTS public_key_hash TEXT,
    ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ;

ALTER TABLE skill_install
    ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE;
