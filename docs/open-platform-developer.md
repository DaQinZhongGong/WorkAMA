# WorkAMA Open Platform 开发者指南

> 契约版本：workama-open-platform-rest-v1
> 本指南描述开放平台 REST API 与事件投递契约。实现对应
> `apps/platform-api/src/workama_platform/modules/open_platform.py`（REST）与
> `modules/external_apps.py`（外部应用执行）。

## 1. 认证与租户隔离

- 开发者凭据：OAuth client（`client_id` + `client_secret`），服务端仅存 `client_secret_hash`
  （KEY_PEPPER 加盐后 SHA-256），比对用常量时间 `hmac.compare_digest`。
- 业务请求携带 `X-Workspace-Id` 头做工作区隔离；JWT（`Authorization: Bearer`）声明角色与能力。

## 2. Webhook 事件投递

事件投递为 REST POST，header：

| 头 | 值 |
| --- | --- |
| `content-type` | `application/json` |
| `user-agent` | `WorkAMA-Webhook/1` |
| `x-workama-event` | 事件类型（如 `webhook.delivery.requested.v1`） |
| `x-workama-signature` | `t=<unix秒>,v1=<HMAC-SHA256 hex>` |
| `idempotency-key` | 幂等键（事件唯一），接收方须按此去重 |

- 签名算法：`v1 = HMAC-SHA256(secret, "{timestamp}.{raw_body}")`，`secret` 为服务端派生的
  peppered secret（`KEY_PEPPER` 参与，第三方持原始 `client_secret` 无法离线复算——
  故 `signature_mutual_trust_verified=false` 是诚实边界）。
- 接收方验签：按 `t` 防重放窗口 + 重算 `v1` 比对；`idempotency-key` 去重。
- 投递语义：重试 + 死信；`payload_too_large` 等错误码见 `open_platform.py`。

## 3. A2A Agent Card 签名

- Agent Card 公钥算法：**Ed25519**（公钥 32 字节，签名 64 字节），存 `pf_a2a_agent_key`。
- 校验：`Ed25519PublicKey.from_public_bytes(raw).verify(...)`；仅支持 Ed25519，
  其他算法 422 拒绝。
- 互操作边界：第三方卡片兼容性/外部互信/外部执行均标 `pending_external`（需真实第三方）。

## 4. 验证边界（诚实声明）

- `provider_execution`：外部 provider 真实执行需真实凭据/第三方环境，本地标 `pending_external`。
- `public_protocol_verified=false`：本地冒烟固定口径，协议级互操作验证属外部 harness。
- 兼容矩阵：`WorkAMA-Docs/925-*.md`（独立设计仓库），本仓不持有，属结构性外部依赖。
- 冒烟证据：`tools/open-platform-smoke.ps1`（PKCE 交换/受控投递/外部排队/`pending_boundary`）。

## 5. 本地验证

```powershell
# 全栈冒烟（open-platform 四契约）
tools/open-platform-smoke.ps1
# 契约门禁
python tools/open_platform_contract_gate.py --json quality/evidence/open-platform-contract-gate.json
```