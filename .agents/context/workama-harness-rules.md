# WorkAMA Harness 规则

> 重建日期：2026-08-19（依据 `.workbuddy/memory/2026-08-01/02` 与 `docs/harness-practice-insights.md` 记录，可考证）。

## 定义

"Harness" 不是组件/目录，而是**契约词汇**：标记「本地无法验证、需外部装置补齐」的边界。
禁止把 Harness 当作可安装组件，也禁止为凑验收伪造 harness 结果。

## 双层验证模型

- 本地冒烟固定：`verification_scope=local-compose` + `public_protocol_verified=false`
- 覆盖状态：`verified_boundary`（本地已证明）/ `pending_boundary`（本地未证明）
- `staging_gate`：命名所需外部 harness 的边界

## 四类 harness（仅文档规划）

1. 外部协议（如 Webhook 签名互操作、OAuth 真实 exchange）
2. 外部 provider（真实 LLM 渠道、第三方 OAuth、A2A 外部互操作）
3. 独立混沌（多 AZ 故障注入、生产式容灾演练）
4. CLI（终端交互场景）

## 机器强制

`tools/open_platform_contract_gate.py`：强制 evidence 含 `staging_gate` 等字段、
扫描 `pending_external` 标记、禁止 secret 落盘。违反即 gate fail。

## 已知 verified_boundary 示例

- webhook.external_http_socket_delivery（本地接收器实证）
- webhook.signature_algorithm_reproducible（HMAC-SHA256(peppered secret_hash)）

## 已知 pending_external 边界（本地不可伪造，须真实第三方/生产环境）

外部 provider 真实执行、OAuth 真实 exchange、A2A 外部互操作、多 AZ 混沌、
二次渗透、海外部署、企业版发版。已固化为 `pending_external` 标记，禁止伪报。