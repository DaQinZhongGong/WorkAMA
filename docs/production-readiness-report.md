# WorkAMA 生产就绪报告（Candidate，待人工 GA 签字）

生成：2026-08-28 / 验证域：`verification_scope=local-compose` + `public_protocol_verified=false` / 交付标准：生产级（非 Demo/POC/MVP）

## 1. 配置中心（可视化最高优先级，实时热生效）

**铁律**：`DB(UI) > ENV(.env) > 代码默认`，非重启类写入即热生效，多进程 ≤1s 收敛，Go 网关经 `configsync` 轮询热下发 LLM 覆盖渠道；`.env` 仅留引导期基础设施（DSN/密钥/服务地址），生产经 secret manager + prod override fail-fast。

- **SCHEMA**：61 项（infra/secrets/auth/oauth/smtp/notify/storage/services/billing/pool/redis_ha/setup/sandbox/llm_staging），类型/范围/正则/必填/密钥掩码齐备，新增项需同步扩展 SCHEMA/前端/测试。
- **热路径**：`_LOCAL` + `REDIS_VERSION_KEY=workama:config:version` + `get_effective_config(ts>1s 或 version≠local)` 跨 worker ≤1s；`load_and_apply_config_overrides()` 启动覆盖 + `config_watcher_loop(interval=1s)` 跨进程收敛；`sandbox-fleet/config_sync.py:ConfigSyncPoller(interval=2s)` + `apps/gateway/internal/configsync` 轮询 `overrides` 视图（仅 DB 覆盖，删除即回落基线）。
- **控制台**：`/admin/platform-config` 可视化分组/搜索/来源徽标(db/env/default)/需重启徽标/未保存徽标/密钥掩码`********`保持语义/连接探针/历史/版本回滚/beforeunload 拦截，批量 PUT 校验落库加密审计发布热生效。
- **测试**：`test_config_center.py`（校验/编解码/优先级/密钥/发布审计回滚/探针）与 `test_config_sync.py`（基线/回落/类型收敛/非 sandbox 过滤/轮询视图/旧版兼容）均为 fake 池/transport，无外设可跑。

## 2. 前端运行时端点注入（同镜像跨环境）

`web/index.html: <script src="/config.js">` 先于模块加载；`web/docker-entrypoint.sh` 启动期把 `WEB_PLATFORM_API_URL/WEB_AGENT_WS_URL/WEB_GRAFANA_URL` 重写为 `window.__WORKAMA_CONFIG__`，未设置键不写入，前端 `config.ts` 按 `runtimeConfig > import.meta.env.VITE_* > 默认` 回退；`miniapp/public/config.js.template` 同理经 `envsubst` 生成。禁止硬编码端点进构建产物。

## 3. 前端生产级精美化

**契约**：`DESIGN.md` Precision Operational（冷静企业控制台，Linear 密度 + Stripe 数据清晰度 + ink-on-stone），`--wama-accent #6d5efc` 主行动色仅作强调，禁止紫蓝渐变/玻璃卡/圆 blob 滥用。

- **设计系统**：`theme.css` 1.6k+ 行，tokens（accent/sidebar/bg/surface/border/text/muted + radius/shadow + Inter/Fraunces/JetBrains Mono + grid）+ 暗色 remap + 组件（按钮/卡片/KPI/表格/表单）已对齐品牌。
- **本次加固**：为配置中心新增 `.cfg-*` 令牌化样式（`cfg-header-meta/version-pill/restart-callout/tabs/search-wrap/field-meta/help/test-result`），tabs 改为可横滚令牌化、dirty 圆点、搜索框 focus ring、测试探针 `ok/fail` 胶囊徽标、重启提示 callout、版本 pill tabular-nums；`admin-platform-config-page.tsx` 去除内联 `borderBottom` 硬编码，接入令牌与无障碍 `aria-selected/role=tablist`。
- **工艺检查**：已按 `frontend-design`/`redesign-skill`/`impeccable-design-polish` 的 `typography/color/anti-ai-slop/state-coverage` 自检：无 generic SaaS 卡片网格、无浮动装饰 blob、无占位 lorem。

## 4. 后端质量与性能硬化

- **契约门禁**：`make contract-check` → `quality/evidence/contract-registry.json` 因缺 `WorkAMA-Docs/720` 契约外部仓库报 `pending_external`（诚实边界），`tools/test_contract_registry.py: 5 tests OK`。
- **文档门禁**：`make docs-check` → `docs-consistency.json` 同因外部文档缺失报 `pending_external`，单测 `2 tests OK`。
- **端口策略**：`tools/port-policy-check.py --compose-dir deploy/compose → {"ok": true}`，三 compose 文件端口均 ∈[20200,20299]。
- **密钥扫描**：`tools/secret-gate.py` 需 `git` 二进制，已在 `python:3.12` 完整镜像中验证通过（本地备选：`docker run python:3.12 sh -c "apt-get update && apt-get install -y git && python tools/secret-gate.py"`）。
- **开放平台契约**：`open_platform_contract_gate → 33 operations, finding: 925 compatibility matrix missing`（待外部矩阵补齐，属 pending_boundary）。

## 5. Docker 生产编排

`make prod-check` 依 `tools/prod_env_check.py` 对 `deploy/compose/.env.production` 做 fail-fast：缺文件/弱值/非法 Fernet 直接拒绝；`docker-compose.prod.yml` 对 `POSTGRES_PASSWORD/JWT_SECRET/INTERNAL_TOKEN/KEY_PEPPER/ENCRYPTION_KEY` 用 `${VAR:?}` 二次兜底。`make prod-up` 为 `prod-check → compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build`。本次 `prod-check` 因未提供生产真值报缺文件（预期），dev 栈用 `deploy/compose/.env` 可 `docker compose up -d --build` 拉起 16 容器验证。

**备份**：`make db-backup`（容器内 `pg_dump -Fc` + `docker cp` 二进制安全）与 `make db-restore FILE=...`（默认 dry-run）已就绪。

## 6. 技能与插件适配

- **mattpocock/skills**：社区 TypeScript 工程化技能集市（`npx skills add` 分发），本次按需引入 `mp-tdd/mp-diagnosing-bugs/mp-code-review/mp-implement/mp-research/mp-wayfinder/mp-to-spec/mp-prototype/mp-domain-modeling` 补强 TDD/诊断/评审/建模/大型任务地图。
- **nexu-io/open-design**：AI 设计系统 + 162 技能工厂，以 `DESIGN.md` 为品牌契约，本次引入 `frontend-design/brand-guidelines/design-review/platform-design/redesign-skill/canvas-design/artifacts-builder/shadcn-ui/design-md/impeccable-design-polish` 补强视觉与制品品质。
- **全局**：`$CODEX_HOME/skills` 38 项（含 curated `figma/pdf/playwright/screenshot/security-*` 等）；**项目级** `.agents/skills` 19 项已恢复并补齐，详见 `docs/skills-integration.md`。

## 7. Git 治理（仅暂存，不提交/推送）

已 `git add` 候选（71 files, +4254/-45）：
- 配置中心：`config_center.py` + `config_sync.py` + `test_config_center.py` + `test_config_sync.py` + `admin-platform-config-page.tsx` + `config.ts` 等
- 运行时注入：`web/docker-entrypoint.sh` + `web/public/config.js` + `miniapp/config.js.template`
- 设计：`DESIGN.md` + `apps/web/DESIGN.md` + `theme.css`(+.cfg) + `craft.css`
- 技能：`.agents/skills/{brand-guidelines,design-review,frontend-design,platform-design,redesign-skill,mp-*,canvas-design,...}` + `docs/skills-integration.md`
- 忽略：`.env/.env.*`（仅 `! .env.example / !.env.production.template` 跟踪）、`quality/evidence/**`、`backups/`、`node_modules/` 等已在 `.gitignore`。

剩余 `?? .claude/.opencode` 属私用上下文，按 `.gitignore` 保持未跟踪。

## 8. 已知边界（诚实声明）

- `WorkAMA-Docs` 外部仓库缺失 → 上述两门禁 live 部分 `pending_external`，本地单测层绿即可合并 candidate。
- 真实第三方 LLM 执行、远程 CI runner、真实 OAuth exchange 等为 `pending_external`。
- GIL-free p99 `make perf-gate` 需真实堆叠与压测装置，本地未声明 `verified_boundary`。
- 多进程 ≤1s 已由 `get_effective_config` 惰性刷新与 `config_watcher_loop` 保证；Go/沙箱侧为 2s 轮询，生产建议按需调至 1s。

## 9. 复现

```bash
docker compose --env-file deploy/compose/.env -f deploy/compose/docker-compose.yml up -d --build
# 冒烟
tools/smoke.ps1  # 登录/chat/assistants（tester@workama.example.com / WorkAMA-Test-2026!）
# 门禁
python tools/port-policy-check.py --compose-dir deploy/compose
docker run --rm -v "$(pwd):/src" -w /src python:3.12 python tools/secret-gate.py
# 可视化配置
open http://localhost:20204/admin/platform-config
```

# 增补：2026-08-28 第二轮硬化（1s 收敛 + 全控制台精美化 + 生产演练）

- **热收敛收紧**：`apps/gateway/cmd/gateway/main.go:Interval 2s→1s`、`apps/gateway/internal/configsync/poller.go:default 2s→1s`、`apps/sandbox-fleet/src/workama_sandbox/config_sync.py:DEFAULT_INTERVAL 2.0→1.0`，多进程与网关/沙箱均满足 ≤1s（平台 API 本就 1s 惰性刷新）。
- **全控制台精美化**：`theme.css` 追加 `cfg-*` 令牌化与 `billing/dash impeccable`（current 计划 accent 顶栏、用量条渐变、stat-card hover 抬升），`billing-page/dashboard` 无需重写即可获生产级密度与微交互。
- **生产演练**：强随机 `INTERNAL_TOKEN/JWT_SECRET/KEY_PEPPER/ENCRYPTION_KEY/POSTGRES_PASSWORD` 预检 `tools/prod_env_check.py → PASS`，`docker compose -f docker-compose.yml -f docker-compose.prod.yml config` 合并校验通过，`port-policy-check → {"ok": true}`（证据 `quality/evidence/prod-check.json`，按 `.gitignore` 保持忽略）。

