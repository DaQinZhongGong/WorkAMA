"""企业版 features 门控模块。

定义企业版功能清单、``/features`` 查询端点、基于
``license_middleware.require_feature`` 的便捷功能门控依赖工厂，以及
``/version`` 版本信息端点。

本模块**只定义依赖工厂与端点**，不强制接入现有端点（接入由主 Agent 后续决定，
避免破坏现有 API）。
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from workama_platform.core import Actor, get_actor
from workama_platform.modules.license_middleware import (
    _fetch_active_license,
    days_remaining,
    license_state,
    require_feature,
)

# 平台版本：从根目录 ``workama_version.py``（单一事实来源）导入。
# 当 platform-api 根目录不在 ``sys.path`` 上时回退到内置常量，
# 保证模块始终可被导入。
try:
    from workama_version import BUILD_DATE, ENTERPRISE_BUILD, PLATFORM_VERSION
except ImportError:  # pragma: no cover - 生产环境需保证根目录在 sys.path
    PLATFORM_VERSION = "v7.176"
    ENTERPRISE_BUILD = True
    BUILD_DATE = "2026-07-31"


router = APIRouter(tags=["enterprise-gating"])


class Features:
    """企业版功能清单。"""

    SSO_SAML = "sso_saml"
    SCIM_SYNC = "scim_sync"
    SIEM_INTEGRATION = "siem_integration"
    LEGAL_HOLD = "legal_hold"
    AUDIT_EXPORT = "audit_export"
    IM_REALTIME = "im_realtime"
    ADVANCED_RAG = "advanced_rag"
    A2A_PROTOCOL = "a2a_protocol"
    WORKSPACE_PORTABILITY = "workspace_portability"
    ENTERPRISE_RBAC = "enterprise_rbac"
    UNLIMITED = "*"


# features 中文描述映射（用于 /features 端点展示）。
FEATURE_DESCRIPTIONS: dict[str, str] = {
    Features.SSO_SAML: "SAML/OIDC 单点登录",
    Features.SCIM_SYNC: "SCIM 用户自动同步",
    Features.SIEM_INTEGRATION: "SIEM 安全事件集成",
    Features.LEGAL_HOLD: "法律留存与合规封存",
    Features.AUDIT_EXPORT: "审计日志导出",
    Features.IM_REALTIME: "IM 实时消息",
    Features.ADVANCED_RAG: "高级 RAG 检索增强",
    Features.A2A_PROTOCOL: "A2A 智能体协议",
    Features.WORKSPACE_PORTABILITY: "工作空间数据可移植",
    Features.ENTERPRISE_RBAC: "企业级 RBAC 权限",
}

# 所有具体功能名（不含通配 ``*``），按定义顺序排列，供 ``available`` 列表使用。
_FEATURE_ORDER: list[str] = [
    Features.SSO_SAML,
    Features.SCIM_SYNC,
    Features.SIEM_INTEGRATION,
    Features.LEGAL_HOLD,
    Features.AUDIT_EXPORT,
    Features.IM_REALTIME,
    Features.ADVANCED_RAG,
    Features.A2A_PROTOCOL,
    Features.WORKSPACE_PORTABILITY,
    Features.ENTERPRISE_RBAC,
]


def _all_feature_names() -> list[str]:
    """返回所有具体功能名（不含 ``*`` 通配）。"""
    return list(_FEATURE_ORDER)


def _normalize_features(features: Any) -> list[str]:
    """将 license 的 ``features`` 字段（list 或 dict）归一化为功能名列表。"""
    if not features:
        return []
    if isinstance(features, dict):
        return [str(k) for k in features.keys()]
    return [str(f) for f in features]


def _available_features() -> list[dict[str, str]]:
    """构造所有可用功能清单（name + description）。"""
    return [
        {"name": name, "description": FEATURE_DESCRIPTIONS.get(name, "")}
        for name in _FEATURE_ORDER
    ]


@router.get("/api/v1/enterprise/features")
async def list_features(actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    """列出当前 license 的 features + 所有可用 features 清单 + 中文描述。

    返回 ``{"licensed": [...], "available": [{"name","description"}], "plan_code": ...}``。
    无 license 时 ``licensed=[]``、``plan_code=None``，``available`` 仍完整返回。
    """
    row = await _fetch_active_license(actor.workspace_id)
    if row is None:
        return {
            "licensed": [],
            "available": _available_features(),
            "plan_code": None,
        }
    return {
        "licensed": _normalize_features(row.get("features")),
        "available": _available_features(),
        "plan_code": row.get("plan_code"),
    }


# ---------------------------------------------------------------------------
# 便捷功能门控依赖（复用 license_middleware.require_feature）
#
# 用法::
#
#     @router.get("/...", dependencies=[Depends(require_sso)])
# ---------------------------------------------------------------------------
require_sso = require_feature(Features.SSO_SAML)
require_scim = require_feature(Features.SCIM_SYNC)
require_siem = require_feature(Features.SIEM_INTEGRATION)
require_legal_hold = require_feature(Features.LEGAL_HOLD)
require_audit_export = require_feature(Features.AUDIT_EXPORT)
require_im_realtime = require_feature(Features.IM_REALTIME)
require_advanced_rag = require_feature(Features.ADVANCED_RAG)
require_a2a = require_feature(Features.A2A_PROTOCOL)
require_workspace_portability = require_feature(Features.WORKSPACE_PORTABILITY)
require_enterprise_rbac = require_feature(Features.ENTERPRISE_RBAC)


@router.get("/api/v1/enterprise/version")
async def enterprise_version(actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    """返回平台版本 + 企业版状态 + license 摘要。

    返回 ``{"platform_version": ..., "enterprise_enabled": ..., "build_date": ...,
    "license": {...} | None, "features": [...]}``。
    无 license 时 ``license=None``、``features=[]``。
    """
    row = await _fetch_active_license(actor.workspace_id)
    if row is None:
        license_summary: dict[str, Any] | None = None
        licensed: list[str] = []
    else:
        license_summary = {
            "license_id": row.get("id"),
            "status": license_state(row),
            "plan_code": row.get("plan_code"),
            "valid_until": row.get("valid_until"),
            "days_remaining": days_remaining(row),
        }
        licensed = _normalize_features(row.get("features"))
    return {
        "platform_version": PLATFORM_VERSION,
        "enterprise_enabled": ENTERPRISE_BUILD,
        "build_date": BUILD_DATE,
        "license": license_summary,
        "features": licensed,
    }
