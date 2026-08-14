"""WorkAMA Webhook 接收端示例（P2 第三方集成）。

使用 Python 标准库 ``http.server`` 实现（无需 Flask 依赖），完整覆盖：

1. HMAC-SHA256 签名校验（``secrets.compare_digest`` 防时序攻击）
2. 事件分发：
   - ``automation.triggered.v1``   自动化触发
   - ``workflow.run.updated.v1``   工作流运行状态变更
   - ``billing.meter_event.v1``    计量计费事件
3. 幂等去重（基于事件 ID 的内存缓存）
4. 可配置监听端口与签名密钥

签名约定（与平台 automation_v2 / audit_exports 一致）：
    请求头 ``X-WorkAMA-Signature``，值为 HMAC-SHA256(secret, raw_body) 的十六进制；
    也兼容 ``sha256=<hex>`` 前缀格式。

运行方式：
    cd examples/python-quickstart
    python webhook_server.py

环境变量：
    WORKAMA_WEBHOOK_SECRET  签名密钥（必填，需与平台触发器配置一致）
    WORKAMA_WEBHOOK_PORT    监听端口，默认 8099
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable


# ---------------------------------------------------------------------------
# 签名校验
# ---------------------------------------------------------------------------


def compute_signature(secret: str, raw_body: bytes) -> str:
    """计算 HMAC-SHA256 十六进制签名。"""
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, raw_body: bytes, provided: str | None) -> bool:
    """校验签名，使用 ``secrets.compare_digest`` 防止时序攻击。

    兼容两种头格式：
      - 纯十六进制：``<hex>``
      - 带前缀：``sha256=<hex>``
    """
    if not provided:
        return False
    expected = compute_signature(secret, raw_body)
    candidate = provided
    # 兼容 sha256=<hex> 前缀格式
    if candidate.startswith("sha256="):
        candidate = candidate[len("sha256="):]
    return hmac.compare_digest(expected, candidate)


# ---------------------------------------------------------------------------
# 事件处理器注册
# ---------------------------------------------------------------------------

# 已处理事件 ID 的内存去重缓存（生产环境建议用 Redis）
_seen_event_ids: set[str] = set()

# 事件类型 -> 处理函数
_HANDLERS: dict[str, Callable[[dict], None]] = {}


def handles(event_type: str) -> Callable[[Callable[[dict], None]], Callable[[dict], None]]:
    """装饰器：注册某类事件的处理函数。"""

    def decorator(fn: Callable[[dict], None]) -> Callable[[dict], None]:
        _HANDLERS[event_type] = fn
        return fn

    return decorator


@handles("automation.triggered.v1")
def _on_automation_triggered(event: dict) -> None:
    """自动化触发事件。"""
    data = event.get("data") or {}
    print(f"[automation.triggered] trigger_id={data.get('trigger_id')} "
          f"run_id={data.get('run_id')} payload={_brief(data.get('payload'))}")


@handles("workflow.run.updated.v1")
def _on_workflow_run_updated(event: dict) -> None:
    """工作流运行状态变更事件。"""
    data = event.get("data") or {}
    print(f"[workflow.run.updated] workflow_id={data.get('workflow_id')} "
          f"run_id={data.get('run_id')} status={data.get('status')} "
          f"error={data.get('error')}")


@handles("billing.meter_event.v1")
def _on_billing_meter(event: dict) -> None:
    """计量计费事件：累加 token / 调用次数等用量。"""
    data = event.get("data") or {}
    print(f"[billing.meter_event] workspace_id={data.get('workspace_id')} "
          f"metric={data.get('metric')} quantity={data.get('quantity')} "
          f"subject={data.get('subject')}")


def dispatch(event: dict) -> bool:
    """分发事件；返回 True 表示已处理，False 表示重复事件。

    约定事件结构：``{"id": "...", "type": "...", "data": {...}}``。
    """
    event_id = str(event.get("id") or "")
    if event_id and event_id in _seen_event_ids:
        print(f"[skip] 重复事件 {event_id}")
        return False
    if event_id:
        _seen_event_ids.add(event_id)
    etype = event.get("type") or event.get("event_type") or ""
    handler = _HANDLERS.get(etype)
    if handler:
        handler(event)
    else:
        print(f"[unhandled] 事件类型 {etype!r} 无注册处理器，原始：{_brief(event)}")
    return True


def _brief(obj: Any, limit: int = 160) -> str:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "...(已截断)"


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------


class WebhookHandler(BaseHTTPRequestHandler):
    """Webhook HTTP 处理器。"""

    # 关闭默认日志，改用自定义输出
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        print(f"[http] {self.address_string()} - {format % args}")

    def do_POST(self) -> None:  # noqa: N802 - stdlib 命名
        secret = os.environ.get("WORKAMA_WEBHOOK_SECRET", "")
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length > 0 else b""

        # 1) 签名校验
        if not secret:
            self._respond(500, {"error": "WORKAMA_WEBHOOK_SECRET not configured"})
            return
        provided = self.headers.get("X-WorkAMA-Signature")
        if not verify_signature(secret, raw_body, provided):
            print("[warn] 签名校验失败，拒绝请求")
            self._respond(401, {"error": "invalid signature"})
            return

        # 2) 解析事件
        try:
            event = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._respond(400, {"error": f"invalid json: {exc}"})
            return

        # 3) 分发（支持单事件与批量事件）
        if isinstance(event, list):
            for ev in event:
                dispatch(ev)
        elif isinstance(event, dict):
            # 批量封装：{"events": [...]}
            if isinstance(event.get("events"), list):
                for ev in event["events"]:
                    dispatch(ev)
            else:
                dispatch(event)
        else:
            self._respond(400, {"error": "unexpected payload shape"})
            return

        self._respond(200, {"accepted": True})

    def do_GET(self) -> None:  # noqa: N802 - stdlib 命名
        # 健康检查端点
        if self.path in ("/", "/healthz"):
            self._respond(200, {"status": "ok", "handlers": sorted(_HANDLERS.keys())})
        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> int:
    secret = os.environ.get("WORKAMA_WEBHOOK_SECRET")
    port = int(os.environ.get("WORKAMA_WEBHOOK_PORT", "8099"))
    if not secret:
        print("[ERR] 请先设置环境变量 WORKAMA_WEBHOOK_SECRET", file=sys.stderr)
        return 2

    server = ThreadingHTTPServer(("0.0.0.0", port), WebhookHandler)
    print(f"[webhook] 监听 0.0.0.0:{port}，已注册事件：{sorted(_HANDLERS.keys())}")
    print("[webhook] 提示：在平台触发器配置中将 Webhook URL 指向本服务，"
          "并使用相同的 secret")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[webhook] 停止服务")
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
