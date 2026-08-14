"""WorkAMA 认证流程完整示例（P2 第三方集成）。

本示例演示完整的认证生命周期，覆盖四种场景：

1. 账号密码登录 → 拿到 access_token（写入 cookie 的 refresh token 由本地 cookiejar 保管）
2. access_token 刷新 → 调用 /api/v1/auth/refresh 续期
3. 登出 → 调用 /api/v1/auth/logout 撤销会话
4. OAuth 授权码流程客户端模拟 → /api/v1/auth/oauth/{provider}/authorize + callback
5. API Key 与 Access Token 两种认证方式对比

为避免引入 requests/httpx，本示例使用标准库 urllib + http.cookiejar 管理 refresh cookie。

运行方式：
    cd examples/python-quickstart
    pip install -e ../../packages/sdk-python
    python auth_flow.py

环境变量：
    WORKAMA_BASE_URL   平台 API 基地址，默认 http://localhost:20200
    WORKAMA_EMAIL      登录邮箱（默认使用测试账号 tester@workama.example.com）
    WORKAMA_PASSWORD   登录密码（默认 WorkAMA-Test-2026!）
    WORKAMA_API_KEY    可选，演示 API Key 认证方式
    WORKAMA_OAUTH_PROVIDER  可选，OAuth 提供商，默认 github
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

from workama_sdk import WorkAMAClient


# ---------------------------------------------------------------------------
# 工具函数：基于 urllib + cookiejar 的原始 HTTP 调用（用于认证端点）
# ---------------------------------------------------------------------------


def _build_opener() -> urllib.request.OpenerDirector:
    """构造一个带 cookiejar 的 opener，自动保存 refresh/access cookie。"""
    jar = CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _post_json(
    opener: urllib.request.OpenerDirector,
    url: str,
    payload: dict | None = None,
    headers: dict | None = None,
    method: str = "POST",
) -> tuple[int, dict | str]:
    """发起一次 JSON 请求并返回 (status, parsed_body)。"""
    data = json.dumps(payload or {}).encode("utf-8")
    req_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "workama-example-auth/0.1.0",
    }
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    try:
        with opener.open(req, timeout=30) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    return status, _parse(raw)


def _parse(raw: bytes) -> dict | str:
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 场景 1/2/3：账号密码登录 → 刷新 → 登出
# ---------------------------------------------------------------------------


def password_login_flow(base_url: str, email: str, password: str) -> dict | None:
    """演示账号密码登录、token 刷新、登出的完整流程。

    平台登录返回 ``{"access_token": ..., "token_type": "bearer", "user": ...}``，
    同时通过 Set-Cookie 下发 ``workama_refresh`` / ``workama_access``。
    刷新接口只认 cookie 里的 refresh token，因此必须复用同一个 opener。
    """
    print("\n=== 1. 账号密码登录 ===")
    opener = _build_opener()
    status, body = _post_json(
        opener,
        f"{base_url}/api/v1/auth/login",
        {"email": email, "password": password},
    )
    print(f"login status={status}")
    if status != 200 or not isinstance(body, dict):
        print(f"[WARN] 登录未成功：{body}（账号可能未校验邮箱或被锁定）")
        return None

    access_token = body.get("access_token")
    user = body.get("user") or {}
    print(f"  user={user.get('email')} workspace_id={user.get('workspace_id')}")
    print(f"  access_token={access_token[:24]}...(已截断)")

    # 使用 access_token 验证会话有效性（复用带 cookie 的 opener）
    try:
        me_status, me_body = _post_json(
            opener,
            f"{base_url}/api/v1/auth/me",
            method="GET",
        )
        print(f"  /auth/me status={me_status} body={_brief(me_body)}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [WARN] /auth/me 调用失败: {exc}")

    print("\n=== 2. Token 刷新 ===")
    status, body = _post_json(opener, f"{base_url}/api/v1/auth/refresh")
    print(f"refresh status={status}")
    if status == 200 and isinstance(body, dict):
        new_token = body.get("access_token")
        print(f"  新 access_token={new_token[:24]}...(已截断)")
        print("  旧 refresh token 已被旋转作废（reuse detection 机制）")
    else:
        print(f"  [WARN] 刷新失败：{body}")

    print("\n=== 3. 登出 ===")
    status, _body = _post_json(opener, f"{base_url}/api/v1/auth/logout")
    print(f"logout status={status}（204 表示成功撤销会话）")
    return {"access_token": access_token, "user": user}


# ---------------------------------------------------------------------------
# 场景 4：OAuth 授权码流程客户端模拟
# ---------------------------------------------------------------------------


def oauth_authorization_code_flow(base_url: str, provider: str) -> None:
    """模拟 OAuth 授权码（Authorization Code + PKCE）流程。

    平台提供两步接口：
      - GET  /api/v1/auth/oauth/{provider}/authorize → 返回 authorization_url
      - GET  /api/v1/auth/oauth/{provider}/callback?code=...&state=... → 换发会话

    真实场景下用户需在浏览器完成第三方授权后回调；本示例只演示第一步拿授权 URL，
    并说明第二步的拼装方式（不真正触发回调，避免污染 state）。
    """
    print("\n=== 4. OAuth 授权码流程（客户端模拟）===")
    opener = _build_opener()
    # 第一步：申请授权 URL（含 PKCE code_challenge 与 state）
    req = urllib.request.Request(
        f"{base_url}/api/v1/auth/oauth/{urllib.parse.quote(provider)}/authorize",
        method="GET",
        headers={"User-Agent": "workama-example-auth/0.1.0"},
    )
    try:
        with opener.open(req, timeout=30) as resp:
            status = resp.status
            body = _parse(resp.read())
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = _parse(exc.read())
    print(f"authorize status={status}")
    if status == 200 and isinstance(body, dict):
        auth_url = body.get("authorization_url")
        print(f"  authorization_url={auth_url}")
        print("  -> 引导用户在浏览器打开该 URL 完成第三方授权")
        print("  -> 第三方回调 WorkAMA 后，平台会再回调 redirect_uri?code=...&state=...")
        # 第二步拼装说明（不真正调用，state 一次性且与 IP/会话绑定）
        callback_url = (
            f"{base_url}/api/v1/auth/oauth/{urllib.parse.quote(provider)}"
            "/callback?code=<授权码>&state=<state>"
        )
        print(f"  第二步 callback 端点: {callback_url}")
    else:
        print(f"  [INFO] 该提供商可能未启用 OAuth：{body}")


# ---------------------------------------------------------------------------
# 场景 5：API Key 与 Access Token 两种认证方式对比
# ---------------------------------------------------------------------------


def compare_auth_modes(base_url: str, access_token: str | None, api_key: str | None) -> None:
    """对比 API Key 与 Access Token 两种 SDK 认证方式。

    - Access Token：以 ``Authorization: Bearer <token>`` 头部发送，优先级更高
    - API Key：以 ``X-WorkAMA-API-Key`` 头部发送，适合长期服务端集成
    两者可分别构造客户端，互不影响。
    """
    print("\n=== 5. API Key vs Access Token 认证对比 ===")
    if access_token:
        c1 = WorkAMAClient(base_url=base_url, access_token=access_token)
        print(f"  [Access Token] client 已构造，将使用 Authorization: Bearer 头")
        _safe_list(c1)
    else:
        print("  [Access Token] 未提供，跳过")

    if api_key:
        c2 = WorkAMAClient(base_url=base_url, api_key=api_key)
        print(f"  [API Key] client 已构造，将使用 X-WorkAMA-API-Key 头")
        _safe_list(c2)
    else:
        print("  [API Key] 未提供 WORKAMA_API_KEY，跳过（API Key 需 owner/admin 角色创建）")

    if access_token and api_key:
        # 同时提供两者时，SDK 优先使用 Bearer Token
        c3 = WorkAMAClient(base_url=base_url, access_token=access_token, api_key=api_key)
        print("  [同时提供] SDK 优先使用 Access Token（Bearer）")


def _safe_list(client: WorkAMAClient) -> None:
    """安全调用 list_workflows，捕获并打印异常，避免示例因 401/403 中断。"""
    try:
        resp = client.list_workflows(limit=3)
        count = len(resp.get("items", []) if isinstance(resp, dict) else [])
        print(f"    list_workflows OK，items={count}")
    except Exception as exc:  # noqa: BLE001
        print(f"    list_workflows 调用返回异常: {exc}")


def _brief(body: object, limit: int = 120) -> str:
    """截断打印响应体，便于日志查看。"""
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "...(已截断)"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    base_url = os.environ.get("WORKAMA_BASE_URL", "http://localhost:20200")
    email = os.environ.get("WORKAMA_EMAIL", "tester@workama.example.com")
    password = os.environ.get("WORKAMA_PASSWORD", "WorkAMA-Test-2026!")
    api_key = os.environ.get("WORKAMA_API_KEY")
    provider = os.environ.get("WORKAMA_OAUTH_PROVIDER", "github")

    result = password_login_flow(base_url, email, password)
    access_token = result["access_token"] if result else None

    oauth_authorization_code_flow(base_url, provider)
    compare_auth_modes(base_url, access_token, api_key)

    print("\n[OK] auth_flow 示例完成")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[中断]", file=sys.stderr)
        raise SystemExit(130)
