"""沙箱网络出网策略模块。

对应《520》§4.2 的网络隔离要求：
- sandbox-base / sandbox-code 镜像默认 ``network_mode="none"``，完全禁止出网
- sandbox-browser 镜像使用自定义 bridge 网络 + 域名白名单，仅允许访问白名单内的域名

域名白名单的强制执行由 sandbox-agentd 在容器内部完成（DNS 过滤 / 代理拦截），
本模块只负责产出 Docker 容器创建时所需的网络配置与白名单元数据。
"""

from __future__ import annotations

# browser 镜像默认可访问的域名白名单
# 通配符 ``*.example.org`` 匹配 ``foo.example.org``，但不匹配裸域 ``example.org``
DEFAULT_ALLOWED_DOMAINS = ["example.com", "*.wikipedia.org", "*.github.com"]

# browser 镜像使用的自定义 bridge 网络名
# 该网络需由部署层（docker-compose / 运维脚本）预先创建
BROWSER_EGRESS_NETWORK = "workama-browser-egress"


def is_domain_allowed(domain: str, allowed_domains: list[str] | None) -> bool:
    """检查域名是否在白名单中，支持通配符匹配。

    匹配规则（大小写不敏感）：
    - 精确匹配：``pattern="example.com"`` 仅匹配 ``example.com``
    - 通配符匹配：``pattern="*.example.com"`` 匹配 ``foo.example.com``、
      ``bar.example.com`` 等子域名，但**不**匹配裸域 ``example.com`` 本身

    Args:
        domain: 待检查的域名
        allowed_domains: 白名单列表；为 ``None`` 时一律拒绝

    Returns:
        ``True`` 表示域名在白名单中
    """
    if allowed_domains is None:
        return False
    domain = (domain or "").lower().strip()
    if not domain:
        return False
    for pattern in allowed_domains:
        pat = (pattern or "").lower().strip()
        if not pat:
            continue
        # 精确匹配
        if pat == domain:
            return True
        # 通配符匹配：*.example.com → 后缀 .example.com
        if pat.startswith("*."):
            suffix = pat[1:]  # ".example.com"
            if domain.endswith(suffix) and len(domain) > len(suffix):
                return True
    return False


def build_egress_rules(image: str = "sandbox-base", allowed_domains: list[str] | None = None) -> dict:
    """构建 Docker 容器出网配置。

    - 非 browser 镜像（sandbox-base / sandbox-code 等）：返回
      ``{"network_mode": "none"}``，完全禁止出网
    - browser 镜像（sandbox-browser）：返回自定义 bridge 网络配置 +
      域名白名单元数据，``network_mode`` 设为 ``None`` 表示改用 ``network`` 字段

    Args:
        image: 沙箱镜像名（sandbox-base / sandbox-browser / sandbox-code 等）
        allowed_domains: 自定义域名白名单；为 ``None`` 时使用
            :data:`DEFAULT_ALLOWED_DOMAINS`

    Returns:
        Docker 网络配置 dict，包含 ``network_mode`` 和可选的自定义网络名
        ``network``、白名单 ``allowed_domains``
    """
    if image != "sandbox-browser":
        return {"network_mode": "none"}
    domains = list(allowed_domains) if allowed_domains is not None else list(DEFAULT_ALLOWED_DOMAINS)
    return {
        "network_mode": None,
        "network": BROWSER_EGRESS_NETWORK,
        "allowed_domains": domains,
    }
