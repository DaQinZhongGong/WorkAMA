"""WorkAMA Agent 对话示例（P2 第三方集成）。

演示：
1. 创建 Agent（助手）→ 拿到 agent_id
2. 通过 SDK ``send_chat_message`` 同步对话，解析 token 用量
3. 使用 SSE 流式调用网关 /v1/chat/completions（OpenAI 兼容）实时接收增量
4. 错误处理 + 指数退避重试（针对 429/5xx）

运行方式：
    cd examples/python-quickstart
    pip install -e ../../packages/sdk-python
    python chat_demo.py

环境变量：
    WORKAMA_BASE_URL       平台 API 基地址，默认 http://localhost:20200
    WORKAMA_ACCESS_TOKEN   Bearer Token（优先）
    WORKAMA_API_KEY        API Key（与 token 二选一）
    WORKAMA_WORKSPACE_ID   可选，工作空间隔离标识
    WORKAMA_AGENT_ID       可选，复用已有 Agent；否则示例会尝试新建
    WORKAMA_GATEWAY_URL    网关地址，默认 http://localhost:20202
    WORKAMA_MODEL          网关对话模型，默认 gpt-4o-mini
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

from workama_sdk import (
    ForbiddenError,
    RateLimitError,
    WorkAMAClient,
    WorkAMAError,
)


# ---------------------------------------------------------------------------
# 1. 创建 Agent（或复用已有）
# ---------------------------------------------------------------------------


def ensure_agent(client: WorkAMAClient, workspace_id: str | None) -> str | None:
    """返回一个可用 agent_id；优先用环境变量，否则新建一个临时助手。"""
    existing = os.environ.get("WORKAMA_AGENT_ID")
    if existing:
        print(f"[INFO] 复用环境变量指定的 Agent: {existing}")
        return existing

    print("\n=== 1. 创建 Agent ===")
    payload = {
        "name": "sdk-chat-demo",
        "description": "Python SDK chat_demo 临时助手",
        "system_prompt": "你是 WorkAMA 助手，请用简洁的中文回答问题。",
        "model": os.environ.get("WORKAMA_MODEL", "gpt-4o-mini"),
        "temperature": 0.5,
        "max_tokens": 512,
        "memory_enabled": False,
        "status": "active",
    }
    try:
        resp = client.create_agent(payload, workspace_id=workspace_id)
    except ForbiddenError as exc:
        print(f"[ERR] 无权创建 Agent（403）: {exc}")
        return None
    except WorkAMAError as exc:
        print(f"[ERR] 创建 Agent 失败: {exc}（可设置 WORKAMA_AGENT_ID 复用已有）")
        return None
    agent_id = resp.get("id") if isinstance(resp, dict) else None
    print(f"  created agent_id={agent_id}")
    return agent_id


# ---------------------------------------------------------------------------
# 2. 同步对话 + token 用量
# ---------------------------------------------------------------------------


def chat_with_retry(
    client: WorkAMAClient,
    agent_id: str,
    message: str,
    workspace_id: str | None,
    max_retries: int = 3,
) -> dict | None:
    """带指数退避重试的同步对话。

    - 429（限流）/ 5xx：重试
    - 401/403/404：不重试，直接返回
    """
    print("\n=== 2. 同步对话（send_chat_message）===")
    delay = 0.5
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.send_chat_message(
                agent_id,
                message,
                conversation_id=f"demo-{agent_id}",
                workspace_id=workspace_id,
            )
            return resp
        except RateLimitError as exc:
            print(f"  [重试 {attempt}/{max_retries}] 触发限流，{delay:.1f}s 后重试: {exc}")
        except WorkAMAError as exc:
            if exc.status_code and 500 <= exc.status_code < 600 and attempt < max_retries:
                print(f"  [重试 {attempt}/{max_retries}] 服务端 {exc.status_code}，{delay:.1f}s 后重试")
            else:
                print(f"[ERR] 对话失败: {exc} (status={exc.status_code})")
                return None
        time.sleep(delay)
        delay *= 2
    print("[ERR] 重试次数耗尽")
    return None


def print_usage(resp: dict) -> None:
    """从助手 run 响应里提取并打印 token 用量统计。"""
    if not isinstance(resp, dict):
        return
    # 平台 assistant_run 响应常见字段：tokens_used / tokens / usage
    usage = resp.get("usage") or {}
    tokens_used = resp.get("tokens_used")
    model = resp.get("model")
    print(f"  run_id={resp.get('id') or resp.get('run_id')}")
    print(f"  model={model}")
    if tokens_used is not None:
        print(f"  tokens_used={tokens_used}")
    if usage:
        print(f"  usage={json.dumps(usage, ensure_ascii=False)}")
    reply = resp.get("assistant_message") or resp.get("message") or resp.get("content") or ""
    if reply:
        print(f"  reply: {str(reply)[:200]}")


# ---------------------------------------------------------------------------
# 3. SSE 流式对话（网关 OpenAI 兼容端点）
# ---------------------------------------------------------------------------


def stream_chat(
    gateway_url: str,
    access_token: str | None,
    api_key: str | None,
    model: str,
    prompt: str,
) -> None:
    """通过网关 /v1/chat/completions 的 SSE 流式增量接收回复。

    使用 urllib 逐行读取 ``text/event-stream``，解析 ``data:`` 行。
    以 ``[DONE]`` 结束；最后一个 chunk 的 usage 包含累计 token 统计。
    """
    print("\n=== 3. SSE 流式对话（gateway /v1/chat/completions）===")
    if not access_token and not api_key:
        print("  [跳过] 未提供凭证，无法调用网关")
        return

    url = gateway_url.rstrip("/") + "/v1/chat/completions"
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        }
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": "workama-example-chat/0.1.0",
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    elif api_key:
        headers["X-WorkAMA-API-Key"] = api_key

    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    collected = ""
    final_usage: dict | None = None
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                choices = chunk.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content") or ""
                    if piece:
                        collected += piece
                        print(piece, end="", flush=True)
                if chunk.get("usage"):
                    final_usage = chunk["usage"]
        print()  # 换行
        print(f"  [流式完成] 累计长度={len(collected)} 字符")
        if final_usage:
            print(f"  [token 用量] {json.dumps(final_usage, ensure_ascii=False)}")
    except urllib.error.HTTPError as exc:
        print(f"\n  [ERR] 网关返回 {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}")
    except urllib.error.URLError as exc:
        print(f"\n  [ERR] 网关连接失败: {exc.reason}（请确认 WORKAMA_GATEWAY_URL 可达）")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    base_url = os.environ.get("WORKAMA_BASE_URL", "http://localhost:20200")
    gateway_url = os.environ.get("WORKAMA_GATEWAY_URL", "http://localhost:20202")
    access_token = os.environ.get("WORKAMA_ACCESS_TOKEN")
    api_key = os.environ.get("WORKAMA_API_KEY")
    workspace_id = os.environ.get("WORKAMA_WORKSPACE_ID")
    model = os.environ.get("WORKAMA_MODEL", "gpt-4o-mini")

    if not access_token and not api_key:
        print("[WARN] 未设置 WORKAMA_ACCESS_TOKEN / WORKAMA_API_KEY，请求可能 401", file=sys.stderr)

    client = WorkAMAClient(base_url=base_url, api_key=api_key, access_token=access_token)

    agent_id = ensure_agent(client, workspace_id)
    if not agent_id:
        print("[INFO] 没有 agent_id，仍演示网关 SSE 流式对话")

    if agent_id:
        resp = chat_with_retry(
            client,
            agent_id,
            "用一句话介绍 WorkAMA 平台的核心能力。",
            workspace_id,
        )
        if resp:
            print_usage(resp)

    # SSE 流式对话（与上面同步对话并列演示）
    stream_chat(gateway_url, access_token, api_key, model, "用三句话描述 RAG 的价值。")

    print("\n[OK] chat_demo 示例完成")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[中断]", file=sys.stderr)
        raise SystemExit(130)
