# WorkAMA 架构上下文

> 重建日期：2026-08-19（依据 `.workbuddy/memory/*` 工作记录与源码盘点，可考证）。

## 形态

pnpm workspace + turbo（前端）+ go.work（Go 1.24：gateway / sandbox-agentd / go-common）。
Python 端（platform-api / agent-server / sandbox-fleet）为 Python 3.12 + FastAPI，镜像内自带依赖。

## 容器拓扑（docker compose，端口 20200-20299，前缀 workama）

| 服务 | 端口 | 职责 |
| --- | --- | --- |
| platform-api | 20200 | 多租户控制面（FastAPI + Granian 4w/8t），/api/v1/* 管理面 + /internal/* 内部面 |
| agent-server | 20201 | AMA-Chat / AMA-Work WebSocket 会话运行时（planner / coordination / tool_runtime） |
| gateway | 20202 | OpenAI 兼容 Go 网关，10 步管道（认证→授权→限流→预算→输入审查→模型映射→路由→转发→输出审查→计量） |
| sandbox-fleet | 20203 | 沙箱编排（docker.sock 挂载，预热池/快照/回收），计量发 NATS |
| web | 20204 | React 控制台（Vite 构建） |
| miniapp | 20205 | 小程序风格 Web 入口 |
| postgres / redis / nats / minio / otel / prometheus / grafana | 20210-20243 | 共享设施：pgvector、JetStream（metering.llm.v1 + WORKAMA_CONTROL）、MinIO 文件、OTel 链路 |

后台进程：`platform-worker`（NATS 计量结算/outbox/自动化/任务/记忆治理）、`rag-worker`（RAG 索引，经 gateway 调 LLM）、`sandbox-agentd`（gVisor/受限容器内 gRPC unix socket 执行器）。

## 关键数据流

1. 管理面：client → platform-api `/api/v1/*`（JWT 鉴权，Redis 限流按 token/IP 分桶，x-internal-token 内部面常量时间比对）。
2. 推理面：client → gateway `/v1/chat/completions`（Bearer API key 或 X-Internal-Token+X-Workspace-ID）→ 10 步管道 →
   上游 LLM 渠道（LLM_STAGING_* 或本地 mock）→ 计量 NATS → platform-worker 结算。
3. 会话面：client WS → agent-server → gateway（LLM）与 sandbox-fleet（工具执行 8 工具，A1-A3 风险分级）。
4. RAG：rag-worker 消费 knowledge 变更 → 经 gateway 调 embedding → pgvector。

## 契约

- `api/openapi.yaml`：P0 纵切（41 path / 60 operationId）。
- `api/asyncapi.yaml`：3 通道（metering.llm.v1、/ws/sessions/{id}、webhook.delivery.requested.v1）。
- `api/runtime-surface.json`：全量运行时盘点（940 路由 / 310 表，机器生成）。
- 门禁：tools/contract_registry_check.py、docs_consistency.py、open_platform_contract_gate.py、port-policy-check.py、secret-gate.py、perf-gate。