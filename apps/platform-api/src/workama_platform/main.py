from __future__ import annotations

import importlib.util
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from workama_observability import configure_observability, install_fastapi

from workama_platform.core import ensure_runtime_schema, pool, redis, settings
from workama_platform.modules.auth.router import router as auth_router
from workama_platform.modules.billing.router import internal_router as billing_internal_router
from workama_platform.modules.billing.router import admin_router as billing_admin_router
from workama_platform.modules.billing.router import router as billing_router
from workama_platform.modules.gateway.router import (
    internal_router as gateway_internal_router,
)
from workama_platform.modules.gateway.router import router as gateway_router
from workama_platform.modules.gateway.internal_channel import ensure_internal_channel
from workama_platform.modules.gateway.relay import router as gateway_relay_router
from workama_platform.modules.gateway_prompts import _ensure_rollout_schema
from workama_platform.modules.gateway_prompts import internal_router as gateway_prompts_internal_router
from workama_platform.modules.gateway_prompts import router as gateway_prompts_router
from workama_platform.modules.notification.router import router as notification_router
from workama_platform.modules.notification.service import ensure_notification_schema
from workama_platform.modules.session.router import public_router
from workama_platform.modules.session.router import router as session_router
from workama_platform.modules.session.router import internal_router as artifact_internal_router
from workama_platform.modules.security.router import internal_router as security_internal_router
from workama_platform.modules.security.router import router as security_router
from workama_platform.modules.security_hardening import (
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    auth_extension_router as security_hardening_auth_router,
    ensure_security_hardening_schema,
    router as security_hardening_router,
)
from workama_platform.modules.privacy.router import public_router as privacy_public_router
from workama_platform.modules.privacy.router import router as privacy_router
from workama_platform.modules.operations import event_router as operations_event_router
from workama_platform.modules.operations import router as operations_router
from workama_platform.modules.jobs import admin_router as jobs_admin_router
from workama_platform.modules.jobs import operation_router as jobs_operation_router
from workama_platform.modules.search import admin_router as search_admin_router
from workama_platform.modules.search import router as search_router
from workama_platform.modules.search import unified_router as search_unified_router
from workama_platform.modules.admin_stats import router as admin_stats_router
from workama_platform.modules.portability import router as portability_router
from workama_platform.modules.platform_support import router as platform_support_router
from workama_platform.modules.knowledge import router as knowledge_router
from workama_platform.modules.knowledge_eval import router as knowledge_eval_router
from workama_platform.modules.rag_eval import router as rag_eval_router
from workama_platform.modules.setup import router as setup_router
from workama_platform.modules.agent_tools import router as agent_tools_router
from workama_platform.modules.approvals import internal_router as approvals_internal_router
from workama_platform.modules.approvals import router as approvals_router
from workama_platform.modules.memory import router as memory_router
from workama_platform.modules.memory_vector import router as memory_vector_router
from workama_platform.modules.device_telemetry import router as device_telemetry_router
from workama_platform.modules.knowledge_base import router as knowledge_base_router
from workama_platform.modules.audit_log import router as audit_log_router
from workama_platform.modules.audit_log import ensure_audit_enterprise_schema
from workama_platform.modules.mcp_server import ensure_builtin_mcp_tools
from workama_platform.modules.mcp_server import router as mcp_server_router
from workama_platform.modules.workspaces import ensure_workspaces_schema
from workama_platform.modules.workspaces import router as workspaces_router
from workama_platform.modules.workflows import ensure_workflow_schema
from workama_platform.modules.workflows import public_router as workflow_public_router
from workama_platform.modules.workflows import router as workflow_router
from workama_platform.modules.subscriptions import ensure_subscription_schema
from workama_platform.modules.subscriptions import router as subscriptions_router
# v7.148: 订阅与计费模块 (billing.py) 与同目录 billing/ 包同名，被包遮蔽，
# 无法通过 ``from workama_platform.modules.billing import ...`` 导入。这里使用
# importlib 按文件路径直接加载，注册到 sys.modules 后取 router 与 ensure_default_plans。
_billing_module_path = Path(__file__).parent / "modules" / "billing.py"
_billing_spec = importlib.util.spec_from_file_location(
    "workama_platform.modules.billing_plans", _billing_module_path
)
billing_plans_module = importlib.util.module_from_spec(_billing_spec)
import sys as _sys  # noqa: PLC0415
_sys.modules[_billing_spec.name] = billing_plans_module
_billing_spec.loader.exec_module(billing_plans_module)
billing_plans_router = billing_plans_module.router
ensure_default_plans = billing_plans_module.ensure_default_plans
# v7.153: 通知中心模块 (notification.py) 与同目录 notification/ 包同名，被包遮蔽，
# 无法通过 ``from workama_platform.modules.notification import ...`` 导入。这里使用
# importlib 按文件路径直接加载（与 billing.py 处理方式一致）。
_notification_module_path = Path(__file__).parent / "modules" / "notification.py"
_notification_spec = importlib.util.spec_from_file_location(
    "workama_platform.modules.notification_center", _notification_module_path
)
notification_center_module = importlib.util.module_from_spec(_notification_spec)
_sys.modules[_notification_spec.name] = notification_center_module
_notification_spec.loader.exec_module(notification_center_module)
notification_center_router = notification_center_module.router
# v7.153: 文件存储模块 (file_storage.py) - 无命名冲突，直接导入
from workama_platform.modules.file_storage import router as file_storage_router
from workama_platform.modules.moderation import ensure_moderation_schema
from workama_platform.modules.moderation import router as moderation_router
from workama_platform.modules.mcp import ensure_mcp_schema
from workama_platform.modules.mcp import router as mcp_router
from workama_platform.modules.enterprise import ensure_enterprise_schema
from workama_platform.modules.enterprise import router as enterprise_router
from workama_platform.modules.code import ensure_code_schema
from workama_platform.modules.code import router as code_router
from workama_platform.modules.code_git_provider import ensure_code_git_provider_schema
from workama_platform.modules.code_git_provider import router as code_git_provider_router
from workama_platform.modules.browser_automation import router as work_browser_router
from workama_platform.modules.work import ensure_work_schema
from workama_platform.modules.work import router as work_router
from workama_platform.modules.passkeys import ensure_passkey_schema
from workama_platform.modules.passkeys import router as passkey_router
from workama_platform.modules.automation import ensure_automation_schema
from workama_platform.modules.automation import router as automation_router
from workama_platform.modules.automation import webhook_router as automation_webhook_router
from workama_platform.modules.automation_v2 import ensure_automation_v2_schema
from workama_platform.modules.automation_v2 import router as automation_v2_router
from workama_platform.modules.automation_v2 import webhook_v2_router as automation_webhook_v2_router
from workama_platform.modules.skills import ensure_skills_schema
from workama_platform.modules.skills import router as skills_router
from workama_platform.modules.skills import skill_installs_router
from workama_platform.modules.connectors import ensure_connectors_schema
from workama_platform.modules.connectors import router as connectors_router
from workama_platform.modules.memory_vector import ensure_memory_semantic_schema
from workama_platform.modules.knowledge_eval import ensure_knowledge_eval_schema
from workama_platform.modules.identity_federation import ensure_identity_federation_schema
from workama_platform.modules.identity_federation import router as identity_federation_router
from workama_platform.modules.identity_federation import scim_router
from workama_platform.modules.open_platform import ensure_open_platform_schema
from workama_platform.modules.open_platform import router as open_platform_router
from workama_platform.modules.open_platform import public_router as open_platform_public_router
from workama_platform.modules.design import ensure_design_schema
from workama_platform.modules.design import router as design_router
from workama_platform.modules.agent_planner import ensure_agent_planner_schema
from workama_platform.modules.agent_planner import router as agent_planner_router
from workama_platform.modules.external_apps import ensure_external_apps_schema
from workama_platform.modules.external_apps import router as external_apps_router
from workama_platform.modules.enterprise_rbac import ensure_enterprise_rbac_schema
from workama_platform.modules.enterprise_rbac import router as enterprise_rbac_router
from workama_platform.modules.audit_exports import audit_export_router
from workama_platform.modules.audit_exports import ensure_audit_export_schema
from workama_platform.modules.audit_exports import router as audit_exports_router
from workama_platform.modules.a2a import ensure_a2a_schema
from workama_platform.modules.a2a import router as a2a_router
from workama_platform.modules.compliance import ensure_compliance_schema
from workama_platform.modules.compliance import router as compliance_router
# v7.175: 企业版 License 校验中间件 + features 门控 + 续费/到期处理
from workama_platform.modules.license_middleware import router as license_middleware_router
from workama_platform.modules.channel_extensions import ensure_channel_extensions_schema
from workama_platform.modules.channel_extensions import public_router as channel_extensions_public_router
from workama_platform.modules.channel_extensions import router as channel_extensions_router
from workama_platform.modules.connectors_v2 import ensure_connectors_v2_schema
from workama_platform.modules.connectors_v2 import router as connectors_v2_router
from workama_platform.modules.observability import router as observability_router
from workama_platform.modules.push_notification import router as push_router
from workama_platform.modules.wechat_miniapp import ensure_wechat_miniapp_schema
from workama_platform.modules.wechat_miniapp import router as wechat_miniapp_router
# v7.174: M7 IM 通道基础模块（内部用户间会话/消息，REST 基础，不含 WebSocket 实时推送）
from workama_platform.modules.messaging import ensure_messaging_schema, router as messaging_router
# v7.179: P3 IM 通道增强——离线消息 + 群组管理 + 消息撤回/编辑
from workama_platform.modules.messaging import im_router as messaging_im_router
# v7.176: 多可用区高可用基础 + 混沌演练降级 + 企业版 features 门控
from workama_platform.modules.resilience import router as resilience_router
from workama_platform.modules.chaos import router as chaos_router
from workama_platform.modules.enterprise_gating import router as enterprise_gating_router
# v7.177: 海外区数据驻留路由 + 安全加固
from workama_platform.modules.data_residency import router as data_residency_router
from workama_platform.modules.data_residency import RegionRoutingMiddleware
from workama_platform.modules.data_residency import compliance_router as data_residency_compliance_router
from workama_platform.modules.data_residency import ensure_data_residency_schema
# v7.150: 工作区深度完善模块（多租户隔离/成员管理/权限矩阵）
# 必须在 workspaces_router 之前挂载，否则既有 workspaces.py 的
# POST/GET /api/v1/workspaces 与 GET /api/v1/workspaces/{id} 会遮蔽本模块端点（FastAPI first-match）。
from workama_platform.modules.workspace import router as workspace_v2_router
# v7.151: 助手/工作流编排模块（整合 gateway LLM + RAG + memory + MCP 工具）
# 必须在 workflow_router 之前挂载，否则既有 workflows.py 的
# POST/GET /api/v1/assistants 与 /api/v1/workflows 等会遮蔽本模块端点（FastAPI first-match）。
from workama_platform.modules.assistant import router as assistant_v2_router
from workama_platform.modules.workflow import router as workflow_v2_router
from workama_platform.modules.skill_market import market_router, agent_skills_router
# v7.178: P2 性能优化专项模块（metrics / slow-queries / cache / benchmark / health-check / query-explain）
from workama_platform.modules.performance import register_middleware as register_performance_middleware
from workama_platform.modules.performance import router as performance_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    await pool.open()
    await ensure_runtime_schema()
    await _ensure_rollout_schema()
    async with pool.connection() as conn:
        async with conn.transaction():
            await ensure_workspaces_schema(conn)
            await ensure_workflow_schema(conn)
            await ensure_subscription_schema(conn)
            # v7.148: 幂等创建 4 个默认套餐（free/starter/pro/enterprise）
            await ensure_default_plans(conn)
            await ensure_moderation_schema(conn)
            await ensure_mcp_schema(conn)
            await ensure_enterprise_schema(conn)
            await ensure_notification_schema(conn)
            await ensure_code_schema(conn)
            await ensure_code_git_provider_schema(conn)
            await ensure_work_schema(conn)
            await ensure_passkey_schema(conn)
            await ensure_automation_schema(conn)
            await ensure_automation_v2_schema(conn)
            await ensure_skills_schema(conn)
            await ensure_connectors_schema(conn)
            await ensure_identity_federation_schema(conn)
            await ensure_open_platform_schema(conn)
            await ensure_design_schema(conn)
            await ensure_agent_planner_schema(conn)
            await ensure_external_apps_schema(conn)
            await ensure_enterprise_rbac_schema(conn)
            await ensure_audit_export_schema(conn)
            await ensure_a2a_schema(conn)
            await ensure_compliance_schema(conn)
            await ensure_channel_extensions_schema(conn)
            await ensure_connectors_v2_schema(conn)
            await ensure_wechat_miniapp_schema(conn)
            await ensure_memory_semantic_schema(conn)
            await ensure_knowledge_eval_schema(conn)
            await ensure_audit_enterprise_schema(conn)
            await ensure_messaging_schema(conn)
            await ensure_security_hardening_schema(conn)
            # v7.177: 海外区数据驻留 schema（data_residency_policy / dsar_request / cross_region_access_audit）
            await ensure_data_residency_schema(conn)
    # v7.145：确保内部 LLM 渠道存在（供 memory_vector 等模块调用 gateway 抽取）。
    # API Key 未配置或 'system' 工作区缺失时只 log warning，不阻断启动。
    await ensure_internal_channel()
    # v7.147：注册内置 MCP 工具（get_current_time/echo/get_workspace_info）。
    # 失败只 log warning，不阻断启动。
    await ensure_builtin_mcp_tools()
    await redis.ping()
    yield
    await redis.aclose()
    await pool.close()


configure_observability("platform-api")

# v7.157: 增强 OpenAPI metadata，覆盖 contact/license/servers/tags_metadata，
# 让 /api/openapi.json 与 Swagger UI (/docs) 能完整呈现 API 概览、模块分组与认证说明。
_API_DESCRIPTION = (
    "WorkAMA Platform API 是 WorkAMA 平台的统一后端服务，对接 15+ 业务模块与 530+ REST 端点，\n"
    "覆盖身份认证、LLM 网关、记忆向量、设备遥测、知识库与 RAG、审计日志、MCP 工具、计费订阅、\n"
    "工作区、助手、工作流编排、通知中心、文件存储、统一搜索与免费供应商目录等核心能力。\n\n"
    "## 基础 URL\n"
    "- Development: `http://localhost:20200`\n"
    "- Production: `https://api.workama.com`\n\n"
    "## 认证方式\n"
    "所有需要鉴权的端点都通过 JWT Bearer Token 进行认证：\n"
    "1. 调用 `POST /api/v1/auth/login`（请求体 `{email, password}`）获取 `access_token`；\n"
    "2. 在后续请求的 `Authorization` 头中携带 `Bearer <access_token>`；\n"
    "3. 跨工作区操作还需要 `X-Workspace-Id: <workspace_id>` 头；\n"
    "4. Token 过期后调用 `POST /api/v1/auth/refresh` 续签；\n"
    "5. 启用 MFA 的账户在登录后会返回 `mfa_required=true` 与 `mfa_ticket`，需调用 MFA 校验端点完成二次验证。\n\n"
    "## 错误响应\n"
    "所有错误统一返回如下 JSON 结构：\n"
    "```json\n"
    '{"detail": "错误描述", "code": "ERROR_CODE"}\n'
    "```\n"
    "常见 HTTP 状态码：\n"
    "- `401 Unauthorized`：未登录或 token 失效\n"
    "- `403 Forbidden`：权限不足或跨工作区访问\n"
    "- `404 Not Found`：资源不存在\n"
    "- `409 Conflict`：资源已存在或状态冲突\n"
    "- `422 Unprocessable Entity`：请求参数校验失败（FastAPI 默认）\n"
    "- `500 Internal Server Error`：服务端异常\n\n"
    "## 测试账号\n"
    "tester@workama.example.com / WorkAMA-Test-2026!（role=owner）"
)

_OPENAPI_TAGS_METADATA = [
    # 任务要求的核心模块 tag
    {"name": "auth", "description": "身份认证与登录、注册、MFA、Token 刷新、OAuth、Passkeys 等账户入口端点。"},
    {"name": "gateway", "description": "LLM 网关：模型路由、频道管理、聊天补全、嵌入、免费供应商目录与启用。"},
    {"name": "memory-vector", "description": "记忆向量模块：长程记忆的写入、召回与提取，基于 pgvector 语义检索。"},
    {"name": "device-telemetry", "description": "设备遥测：注册设备、上报遥测事件与心跳、查询设备状态。"},
    {"name": "knowledge-base", "description": "知识库与 RAG：知识库 CRUD、文档上传、分块、嵌入索引与 RAG 查询。"},
    {"name": "audit-log", "description": "审计日志：记录关键操作行为并支持按条件检索与导出。"},
    {"name": "mcp", "description": "Model Context Protocol：MCP 工具注册、manifest、调用与企业工具治理。"},
    {"name": "billing", "description": "订阅与计费：套餐（plans）、订阅、账户余额、用量统计与发票。"},
    {"name": "workspace", "description": "工作区（v2）：多租户隔离、成员管理、邀请、权限矩阵。"},
    {"name": "assistant", "description": "助手编排：助手 CRUD、run、clone，集成 LLM + RAG + memory + MCP 工具。"},
    {"name": "workflow", "description": "工作流编排：7 种节点类型（llm_call/tool_call/rag_query 等）的 DAG 工作流。"},
    {"name": "notification", "description": "通知中心：in-app 通知 CRUD、未读计数、批量已读。"},
    {"name": "file-storage", "description": "文件存储：multipart 上传、下载、复制、软删除、按 kind 统计。"},
    {"name": "search", "description": "统一搜索：跨多张业务表（assistant/workflow/knowledge_base/file/notification）ILIKE 聚合。"},
    {"name": "free-providers", "description": "免费供应商目录：100 个内置免费 LLM 供应商，支持查询与一键启用。"},
    # 兼容实际路由已使用的 tag（避免 Swagger UI 出现无描述的 tag）
    {"name": "identity", "description": "身份与登录（auth router 使用的 tag）：login/register/refresh/oauth/passkeys。"},
    {"name": "identity-federation", "description": "身份联邦：SAML SSO、OIDC、SCIM 用户同步。"},
    {"name": "enterprise-identity", "description": "企业身份：组织、SSO 配置、域名校验。"},
    {"name": "enterprise-rbac", "description": "企业 RBAC：角色、权限、策略与组成员管理。"},
    {"name": "enterprise-compliance", "description": "企业合规：合规策略、数据保留、合规报告。"},
    {"name": "sessions", "description": "会话管理：会话创建、列表、消息追加与产物查询。"},
    {"name": "subscriptions", "description": "订阅管理：订阅创建、升降级、续费与取消。"},
    {"name": "knowledge", "description": "知识管理（旧版）：知识库与文档元数据。"},
    {"name": "knowledge-evaluation", "description": "知识库评测：评测集、评测任务与质量指标。"},
    {"name": "rag-evaluation", "description": "RAG 评测：检索质量、生成质量与端到端评测。"},
    {"name": "assistants-workflows", "description": "助手与工作流（聚合 tag）：v1 时代的助手/工作流编排。"},
    {"name": "external-apps", "description": "外部应用：第三方应用接入与回调。"},
    {"name": "marketplace", "description": "应用市场：模板、技能与插件市场。"},
    {"name": "channel-extensions", "description": "渠道扩展：多渠道接入与消息扩展。"},
    {"name": "open-platform", "description": "开放平台：开放 API、应用凭证与权限授权。"},
    {"name": "ama-work", "description": "AMA-Work：桌面客户端与工作端集成。"},
    {"name": "ama-work-browser", "description": "AMA-Work 浏览器自动化：浏览器会话与脚本执行。"},
    {"name": "ama-design", "description": "AMA Design：设计稿、设计 token 与组件库管理。"},
    {"name": "workspaces", "description": "工作区（v1）：基础工作区 CRUD。"},
    {"name": "workspace-portability", "description": "工作区可移植性：导入、导出与迁移。"},
    {"name": "platform-support", "description": "平台支撑：公告、健康检查、版本信息。"},
    {"name": "platform-jobs", "description": "平台作业：异步任务、死信队列与重试。"},
    {"name": "async-operations", "description": "异步操作：长任务统一抽象与状态查询。"},
    {"name": "operations-governance", "description": "运营治理：事件目录、发布证据与配置回滚。"},
    {"name": "search-operations", "description": "搜索运营：搜索索引重建与状态。"},
    {"name": "code", "description": "代码模块：代码片段、模板与执行。"},
    {"name": "code-git-provider", "description": "Git 提供商：GitHub/GitLab/Bitbucket 集成。"},
    {"name": "scim", "description": "SCIM 协议：用户与组的标准化同步。"},
    {"name": "audit-exports", "description": "审计导出：审计数据导出任务与下载。"},
    {"name": "memory", "description": "记忆模块（旧版）：基础记忆 CRUD。"},
    {"name": "privacy", "description": "隐私与合规：处理活动、同意、数据请求与删除墓碑。"},
    {"name": "notification-center", "description": "通知中心（独立 tag）：notification 表上的通知 CRUD。"},
    {"name": "notifications", "description": "通知（旧版）：通知偏好、模板与投递记录。"},
    {"name": "automations", "description": "自动化：触发器、动作与 webhook 自动化。"},
    {"name": "siem", "description": "SIEM：安全事件与日志集成。"},
    {"name": "a2a", "description": "Agent-to-Agent：智能体互操作协议。"},
    {"name": "approvals", "description": "审批流：审批定义、实例与任务。"},
    {"name": "approvals-internal", "description": "审批流（内部）：内部审批调用。"},
    {"name": "skills", "description": "技能：技能注册、安装与调用。"},
    {"name": "skill-installs", "description": "技能安装：技能安装记录与管理。"},
    {"name": "gateway-prompts", "description": "网关 Prompt：Prompt 模板与版本管理。"},
    {"name": "gateway-internal", "description": "网关（内部）：内部 LLM 调用与渠道管理。"},
    {"name": "billing-internal", "description": "计费（内部）：内部计量与结算调用。"},
    {"name": "billing-admin", "description": "计费（管理）：计费管理端操作。"},
    {"name": "security", "description": "安全：API Key、密钥管理与安全策略。"},
    {"name": "moderation", "description": "内容审核：文本/图片审核策略与结果。"},
    {"name": "connectors", "description": "连接器：第三方数据源与 SaaS 集成。"},
    {"name": "passkeys", "description": "Passkeys：WebAuthn 无密码认证。"},
    {"name": "unified-search", "description": "统一搜索（独立 tag）：跨表 ILIKE 聚合。"},
    {"name": "push", "description": "PWA Web Push：推送订阅管理与发送。"},
    {"name": "wechat-miniapp", "description": "微信小程序登录闭环：code2Session 登录、session 持久化、订阅消息授权与发送、模板管理。"},
    {"name": "system", "description": "系统：健康检查（healthz/readyz）与系统元数据。"},
    {"name": "messaging", "description": "IM 通道基础：会话创建/列表/退出、消息发送/列表、已读标记、WebSocket 实时推送。"},
    {"name": "im", "description": "IM 通道增强：离线消息存储与拉取、群组管理（owner/admin/member 角色）、消息撤回与编辑（5 分钟时间窗口 + 审计日志）。"},
]

app = FastAPI(
    title="WorkAMA Platform API",
    description=_API_DESCRIPTION,
    version="v7.157",
    lifespan=lifespan,
    openapi_url="/api/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
    contact={
        "name": "WorkAMA Platform Team",
        "email": "platform@workama.com",
        "url": "https://github.com/workama/workama",
    },
    license_info={
        "name": "MIT",
        "url": "https://opensource.org/license/mit",
    },
    servers=[
        {"url": "http://localhost:20200", "description": "Development"},
        {"url": "https://api.workama.com", "description": "Production"},
    ],
    openapi_tags=_OPENAPI_TAGS_METADATA,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# v7.178: P2 性能优化专项——慢查询日志中间件（记录超过 200ms 的请求到 ops_slow_query_log）
register_performance_middleware(app)
# P3 第二次渗透测试安全加固——速率限制（内层）+ 安全响应头（外层，确保 429 也带安全头）
# Starlette add_middleware 为 LIFO：后注册的更外层，故先注册 RateLimit 再注册 SecurityHeaders，
# 使 SecurityHeadersMiddleware 包裹 RateLimitMiddleware，所有响应（含 429）均注入 6 个安全头。
app.add_middleware(RateLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)


install_fastapi(app, "platform-api")


app.include_router(privacy_router)
app.include_router(privacy_public_router)
app.include_router(operations_router)
app.include_router(operations_event_router)
app.include_router(jobs_operation_router)
app.include_router(jobs_admin_router)
app.include_router(search_router)
app.include_router(search_admin_router)
# v7.264: /admin 首页统计聚合端点（修复 /api/v1/admin/stats 404）
app.include_router(admin_stats_router)
# v7.153: 统一搜索（直接 ILIKE 跨表聚合，独立于 ops_search_document 投影）
app.include_router(search_unified_router)
# v7.153: 通知中心（独立于既有 notification 包，使用 notification 表）
app.include_router(notification_center_router)
# v7.153: 文件存储（MinIO mock，元数据存 file_metadata 表）
app.include_router(file_storage_router)
app.include_router(portability_router)
app.include_router(platform_support_router)
app.include_router(knowledge_router)
app.include_router(knowledge_eval_router)
app.include_router(rag_eval_router)
app.include_router(setup_router)
app.include_router(agent_tools_router)
app.include_router(approvals_router)
app.include_router(approvals_internal_router)
app.include_router(memory_router)
app.include_router(memory_vector_router)
app.include_router(device_telemetry_router)
app.include_router(knowledge_base_router)
app.include_router(audit_log_router)
app.include_router(mcp_server_router)
app.include_router(workspace_v2_router)
app.include_router(workspaces_router)
# v7.151: 助手/工作流编排模块必须在既有 workflow_router 之前挂载，
# 否则既有 workflows.py 的 POST/GET /api/v1/assistants 与
# /api/v1/workflows 等会遮蔽本模块同名端点（FastAPI first-match）。
app.include_router(assistant_v2_router)
app.include_router(workflow_v2_router)
app.include_router(workflow_router)
app.include_router(workflow_public_router)
# v7.148: billing_plans_router 必须在 subscriptions_router 之前挂载，
# 否则 subscriptions.py 的 GET /api/v1/billing/plans 等路由会遮蔽本模块的同名端点。
app.include_router(billing_plans_router)
app.include_router(subscriptions_router)
app.include_router(moderation_router)
app.include_router(mcp_router)
app.include_router(enterprise_router)
app.include_router(code_router)
app.include_router(code_git_provider_router)
app.include_router(work_router)
app.include_router(work_browser_router)
app.include_router(passkey_router)
app.include_router(automation_router)
app.include_router(automation_webhook_router)
app.include_router(automation_v2_router)
app.include_router(automation_webhook_v2_router)
app.include_router(skills_router)
app.include_router(skill_installs_router)
# v7.161: 技能市场与 Agent 技能挂载
app.include_router(market_router)
app.include_router(agent_skills_router)
app.include_router(connectors_router)
app.include_router(identity_federation_router)
app.include_router(scim_router)
app.include_router(open_platform_router)
app.include_router(open_platform_public_router)
app.include_router(design_router)
app.include_router(agent_planner_router)
app.include_router(external_apps_router)
app.include_router(enterprise_rbac_router)
app.include_router(audit_exports_router)
app.include_router(audit_export_router)
app.include_router(a2a_router)
app.include_router(compliance_router)
app.include_router(license_middleware_router)
# v7.177: 海外区数据驻留路由（在 compliance_router 之前注册避免路径遮蔽）
app.include_router(data_residency_router)
app.include_router(data_residency_compliance_router)
app.include_router(channel_extensions_router)
app.include_router(channel_extensions_public_router)
app.include_router(connectors_v2_router)
app.include_router(observability_router)
app.include_router(push_router)
app.include_router(wechat_miniapp_router)
app.include_router(messaging_router)
# v7.179: P3 IM 通道增强——离线消息 + 群组管理 + 消息撤回/编辑
app.include_router(messaging_im_router)
# v7.176: 高可用 + 混沌 + 企业版门控
app.include_router(resilience_router)
app.include_router(chaos_router)
app.include_router(enterprise_gating_router)
app.include_router(auth_router)
app.include_router(gateway_router)
app.include_router(gateway_internal_router)
app.include_router(gateway_relay_router)
app.include_router(gateway_prompts_router)
app.include_router(gateway_prompts_internal_router)
app.include_router(billing_router)
app.include_router(billing_internal_router)
app.include_router(billing_admin_router)
app.include_router(notification_router)
app.include_router(security_router)
app.include_router(security_internal_router)
# P3 第二次渗透测试安全加固路由（CSRF token / 审计链校验 / 密码强度 / 刷新令牌轮换）
app.include_router(security_hardening_router)
app.include_router(security_hardening_auth_router)
app.include_router(session_router)
app.include_router(artifact_internal_router)
app.include_router(public_router)
# v7.178: P2 性能优化专项路由（metrics / slow-queries / cache / benchmark / health-check / query-explain）
app.include_router(performance_router)


@app.get("/healthz", tags=["system"])
async def healthz():
    # 性能优化：健康探针短路，不查 DB、不经过业务依赖，直接返回存活信号。
    # 禁止缓存，避免负载均衡器/探针拿到过期状态。
    return JSONResponse(
        {"status": "ok", "service": "platform-api"},
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/readyz", tags=["system"])
async def readyz():
    # 就绪探针：仅做轻量依赖 ping（DB + Redis），不执行 schema 检查或业务查询
    async with pool.connection() as conn:
        await conn.execute("SELECT 1")
    await redis.ping()
    return {"status": "ready", "service": "platform-api"}
