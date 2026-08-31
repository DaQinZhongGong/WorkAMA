# 计费域生产级投产计划 — Billing Production Plan (v1.0, 2026-08-29)

> **目标**：以生产级（非 Demo/POC/MVP）标准交付计费域闭环，工程高性能与前端精美 UI/UX 同等重要；所有可运维配置经可视化界面（`DB(UI) > ENV > 代码默认`）热生效；全程 docker-compose 验证；AI 协作工程纪律×设计审美可持续演进。  
> **授权**：用户已签字全权处理，每轮验证无误后 `git add` 暂存（不 commit/push，待人工审核）。  
> **方法**：文档驱动 + 双循环 tracer bullet（`grill-with-docs → to-spec → to-tickets → implement(tdd+code-review) → wayfinder` ↔ `design-brief → brand-extract → frontend-design/stitch → plan-design-review → deliver`），本地能证明的才声明。

---

## 1. 背景与问题

- 前端 `billing-page.tsx` 调用 `GET /api/v1/billing/overview`，但后端 `billing.py` 仅暴露 `plans/subscriptions/usage/invoices` 12 端点，`openapi.yaml` 亦仅定义 `/billing/account` + `/billing/transactions`，存在**契约断层** → 前端当前依赖 mock / 空数据，无法投产。
- 计费虽有 `bill_*` 双池账本（冻结/结算/释放）与 `billing_plan/subscription/usage_record/invoice` 四表，但缺少**聚合概览**（当前订阅 KPI + 套餐对比 + 用量进度 + 最近计费事件 + 配额检查）与**可视化**。
- 配置中心已实现 DB>ENV>默认、Fernet 加密、revision 快照、Redis 版本热生效、Gateway configsync 轮询，但需验证**所有可运维配置确已走 UI**（`.env` 仅留 DSN/密钥材料/服务地址，且生产经 secret manager + prod override fail-fast）。
- 设计系统已定 `DESIGN.md Precision Operational`（Linear 密度 + Stripe 财务清晰 + ink-on-stone），但计费页仍停留“卡片+进度条” demo 形态，缺 D3 数据可视化、空状态、a11y、性能门禁。

## 2. 目标与非目标

**目标（生产级验收）**
- P0 契约一致：`openapi.yaml` + `runtime-surface.json` + 前后端契约注册表绿色。
- P0 功能闭环：`GET /overview` 聚合（plans + active subscription + period usage + recent events + quota）、`check_quota` 接入网关计量、`ensure_default_plans` 幂等、账本准确（Decimal、无 float）。
- P0 配置：所有计费/限流/SMTP/OAuth/存储等可运维项均经 `/admin/platform-config` 可视化编辑，非重启类 ≤1s 收敛，Gateway LLM 覆盖渠道经 `configsync` 热下发；`.env` 不承载业务配置。
- P0 性能：`platform-api` p99 < 300ms（本地 compose 4w/8t Granian），前端 `tsc --noEmit` + `vitest` + `axe` 无 critical，Lighthouse 性能 ≥90。
- P0 设计：按 `DESIGN.md` §9 checklist（320/760/980/1100 断点、hover/focus/active/disabled 全态、axe、tabular-nums、anti-slop）。

**非目标**
- 不引入 K8S、外部 CI runner、真实第三方 LLM / OAuth 远程 harness（标 `pending_external`）。
- 不改 `workama-*` 端口段 `[20200,20299]`，不另起宿主端口。

## 3. 范围与交付物

| 域 | 交付物 | 证据 |
|---|---|---|
| 后端 | `GET /api/v1/billing/overview`（聚合+缓存+鉴权+配额）、`openapi.yaml` 补齐、`runtime-surface` 机器生成、`tools/contract_registry_check` 绿色 | `quality/evidence/contract-registry.json`、单测、docker 日志 |
| 前端 | 重塑 `billing-page.tsx`（KPI 4 宫格 + 套餐对比 + 用量进度 D3 微图 + 事件流 + 订阅详情）、D3/frame-data-chart-nyt 微件、`billing-page.test.tsx` 扩充、a11y/响应式 | `tsc --noEmit`、`vitest`、`axe`、`quality/evidence/design-review.md` |
| 配置 | 校验 `config_center.SCHEMA` 中 `billing.*` + `rate_limit_*` 等走 UI，新增项同步 SCHEMA+前端+测试，产出 `quality/evidence/config-center-audit.json` | `admin-platform-config-page.test.tsx`、`GET /api/v1/config/values` |
| 设计系统 | `DESIGN.md` 精度校验、`packages/ui` token 复用、`.agents/design-systems/registry.json` 登记 | `quality/evidence/design-tokens.json` |
| 可持续 | `workama-setup`/`workama-router`/`workama-design-system` 注册、`skills/manifest.json` 更新 | `quality/evidence/skills-install-manifest.json` |

## 4. 方案设计

### 4.1 后端聚合端点

- **路径**：`GET /api/v1/billing/overview`（需鉴权 `get_actor`，返回 `BillingOverview`）。
  ```json
  {
    "plans": Plan[], "subscription": Subscription|null, "usage": UsageSummary,
    "events": BillingEvent[] (近 8 条), "quota": {metric: check_quota 结果},
    "period": {"start": "...", "end": "..."}, "counts": {"requests":123, "tokens":456}
  }
  ```
- **逻辑**：复用 `ensure_default_plans` + `list_plans` 缓存键 `_global_:billing_plans:active`；`active subscription` 取 `workspace_id` 最新 `status='active'`；`usage` 汇总 `bill_usage_record` + `bill_usage_hourly` 当前周期；`events` 取 `bill_transaction`；`quota` 调 `check_quota(metric=tokens_used/seats_used/storage_used_gb)`。
- **性能**：`pool.connection()` 单次事务内 4 查询并发（`asyncio.gather`），结果进 `cache_set` 60s，`cache_delete` 在 `create_plan`/`subscription` 变更时失效。
- **契约**：`openapi.yaml` 新增 `getBillingOverview`，`tools/contract_registry_check.py` 与 `docs_consistency.py` 保持一致；`api/runtime-surface.json` 重生成。

### 4.2 前端重塑

- **信息架构**：保持 `eyebrow + H1 + refresh` 页头；KPI 4 宫格（当前套餐/请求数/tokens/积分）用 `Kpi` 组件 + `tabular-nums`；`Panel` 双栏：左套餐对比（`resource-grid` 3 列响应式，`is-current` 高亮 + `Status`）、右纵列（用量进度条 70/90 分界 + 迷你 D3 sparkline + 最近事件流 + 订阅详情）。
- **可视化**：`od-d3-visualization` + `od-frame-data-chart-nyt` 微件（请求/tokens 30 天 sparkline，`bill_usage_hourly` 驱动，空状态走 `StateView` 插画）。
- **交互**：`viewQuote` 调用 `POST /plans/{id}/quote`（已存在）并走审批流占位；刷新按钮 `data-testid=billing-refresh` 保持；所有按钮含 hover/focus/active/disabled，表格/列表键盘可达。
- **验证**：`pnpm --filter @workama/web test`（扩充 overview 聚合、空状态、千分位、货币）、`tsc --noEmit`、`axe`（无 critical）、4 断点无溢出。

### 4.3 配置中心

- 新增可配置项走 `SCHEMA` + `GROUP_LABELS` + 前端 `admin-platform-config-page` Tab + 测试；优先级 `DB>ENV>默认`；非重启类写入即覆盖 `core.settings` 单例并 `redis:workama:config:version` 递增，其他 worker ≤1s 轮询收敛；Go 网关经 `configsync` 热下发 `llm_staging_*`。
- `.env` 仅保留 `database_url/redis_url/nats_url/jwt_secret/key_pepper/encryption_key/internal_token` 等引导期基础设施，生产经 `deploy/compose/.env.production` + `docker-compose.prod.yml` fail-fast。

## 5. 任务拆解（tracer-bullet，每票含 blocking edges）

- T1: Phase 0 基线 — docker 栈拉起 + 6 门禁 + 端口/密钥门禁（block: 无）
- T2: Phase 1 配置中心审计 — SCHEMA 覆盖度 + UI 冒烟 + Gateway 热下发验证（block: T1）
- T3: 后端 overview 实现 + 单测 + openapi 契约（block: T1）
- T4: 前端 billing 重塑 + D3 微件 + 测试/a11y（block: T3 原型契约）
- T5: 全量门禁 + 证据链 + git 暂存 + 可持续注册（block: T2, T4）
- T6: 可选 — 将 `dashboard`/`live-dashboard` 模板按需沉淀为 `billing` 可复用组件（block: T4）

## 6. 验证计划（双层模型）

- **本地 verified_boundary**：`make contract-check`、`make docs-check`、`make test`（pytest+go test+vitest+manifest）、`make smoke`（登录/chat/assistants/billing overview）、`make perf-gate`（GIL-free p99）、`make secret-gate`、`python tools/port-policy-check.py`、`tsc --noEmit`、`axe`。
- **pending_boundary（诚实标记）**：真实 LLM 渠道、OAuth 真实 exchange、多 AZ 混沌、远程 CI — `quality/evidence/*.json` 中 `staging_gate` 字段注明，不伪造。
- **证据落点**：`quality/evidence/contract-registry.json`、`docs-consistency.json`、`open-platform-contract-gate.json`、`skills-install-manifest.json`、`config-center-audit.json`、`design-review.md`。

## 7. 风险与缓解

| 风险 | 缓解 |
|---|---|
| `billing.py` 与 `billing/` 包同名导入遮蔽 | 保持 `importlib.util.spec_from_file_location` 加载路径，不改包结构 |
| `overview` 缓存与订阅变更不一致 | `create/subscription/usage` 写路径 `cache_delete(_cache_key(...))` |
| 前端硬编码端点 | 经 `apps/web/src/config.ts` + `/config.js` 运行时注入，同镜像跨环境 |
| .env 误承载业务配置 | `tools/prod_env_check.py` fail-fast + `config_center` 审计脚本 |

## 8. 里程碑

- M1（Phase 0+1）：本地栈绿 + 配置中心审计报告
- M2（Phase 2+3）：前后端计费闭环 + 测试/设计审美绿
- M3（Phase 4）：全量门禁绿 + 证据链 + `git add` 暂存 + `manifest` 更新

---

*本计划即 `to-spec` 产物，后续 `to-tickets` 将按 §5 逐票生成 `quality/tickets/<id>-<slug>.md`，`implement` 阶段以 TDD 逐 seams 落地。*
