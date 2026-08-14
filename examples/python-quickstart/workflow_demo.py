"""WorkAMA 工作流编排示例（P2 第三方集成）。

演示：
1. 构造包含 HTTP 节点 / Code 节点 / Branch（condition）节点的工作流图
2. 创建工作流 → 触发运行（POST /api/v1/workflows/{id}/runs）
3. 拉取运行事件流（GET /api/v1/workflows/{id}/runs/{run_id}/events）
4. 打印运行结果与节点级事件

运行方式：
    cd examples/python-quickstart
    pip install -e ../../packages/sdk-python
    python workflow_demo.py

环境变量：
    WORKAMA_BASE_URL       平台 API 基地址，默认 http://localhost:20200
    WORKAMA_ACCESS_TOKEN   Bearer Token（优先）
    WORKAMA_API_KEY        API Key（与 token 二选一）
    WORKAMA_WORKSPACE_ID   可选，工作空间隔离标识
    WORKAMA_WORKFLOW_ID    可选，复用已有工作流；否则示例会新建

注意：
    - HTTP 节点使用真实 http(s) 端点（如 https://httpbin.org/get）
    - Code 节点运行在受限沙箱：禁止 import / eval / exec / open / dunder 访问
    - Branch 节点对应平台 ``condition`` 类型，按条件表达式分流
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
    WorkAMAClient,
    WorkAMAError,
)


# ---------------------------------------------------------------------------
# 1. 构造工作流图（HTTP + Code + Branch + Input/Output）
# ---------------------------------------------------------------------------


def build_demo_graph() -> dict:
    """构造演示用 DAG。

    结构：
        input -> http(httpbin/get) -> code(对 status 翻倍) -> condition(分支) -> output

    平台 graph schema：
        nodes: [{id, type, config}, ...]，至少包含一个 input 与一个 output
        edges: [{source, target}, ...]，必须无环
    节点类型：input / output / http / code / condition（Branch）等。
    """
    nodes = [
        {
            "id": "n_input",
            "type": "input",
            "config": {"schema": {"topic": "string"}},
        },
        # HTTP 节点：真实 http(s) 调用，GET httpbin.org/get
        {
            "id": "n_http",
            "type": "http",
            "config": {
                "url": "https://httpbin.org/get",
                "method": "GET",
                "timeout": 20,
                "headers": {"Accept": "application/json"},
            },
        },
        # Code 节点：受限 Python 沙箱，对上游 status 做简单运算
        {
            "id": "n_code",
            "type": "code",
            "config": {
                "language": "python",
                # 禁止 import / eval / exec / open / dunder；只能纯计算
                "code": "result = {'doubled': int(inputs.get('value', 200)) * 2}",
            },
        },
        # Branch 节点（平台 condition 类型）：按表达式分流
        {
            "id": "n_branch",
            "type": "condition",
            "config": {
                "cases": [
                    {"id": "high", "condition": "value > 300"},
                    {"id": "low", "condition": "value <= 300"},
                ],
            },
        },
        {
            "id": "n_output",
            "type": "output",
            "config": {"format": "json"},
        },
    ]
    edges = [
        {"source": "n_input", "target": "n_http"},
        {"source": "n_http", "target": "n_code"},
        {"source": "n_code", "target": "n_branch"},
        {"source": "n_branch", "target": "n_output"},
    ]
    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# 2. 创建 / 复用工作流
# ---------------------------------------------------------------------------


def ensure_workflow(client: WorkAMAClient, workspace_id: str | None) -> str | None:
    existing = os.environ.get("WORKAMA_WORKFLOW_ID")
    if existing:
        print(f"[INFO] 复用环境变量指定的工作流: {existing}")
        return existing

    print("\n=== 1. 创建工作流 ===")
    payload = {
        "name": "sdk-workflow-demo",
        "description": "Python SDK workflow_demo：HTTP + Code + Branch 节点",
        "graph": build_demo_graph(),
    }
    try:
        resp = client.create_workflow(payload, workspace_id=workspace_id)
    except ForbiddenError as exc:
        print(f"[ERR] 无权创建工作流（403）: {exc}")
        return None
    except WorkAMAError as exc:
        # 422 表示 graph 校验失败，打印 detail 便于排错
        print(f"[ERR] 创建工作流失败: {exc} (status={exc.status_code})")
        if exc.body:
            print(f"  detail: {json.dumps(exc.body, ensure_ascii=False)[:300]}")
        return None
    wf_id = resp.get("id") if isinstance(resp, dict) else None
    print(f"  created workflow_id={wf_id}")
    return wf_id


# ---------------------------------------------------------------------------
# 3. 触发运行
# ---------------------------------------------------------------------------


def trigger_run(
    client: WorkAMAClient,
    workflow_id: str,
    inputs: dict,
    workspace_id: str | None,
) -> dict | None:
    print("\n=== 2. 触发运行 ===")
    print(f"  inputs={json.dumps(inputs, ensure_ascii=False)}")
    try:
        resp = client.run_workflow(workflow_id, inputs, workspace_id=workspace_id)
    except WorkAMAError as exc:
        print(f"[ERR] 触发运行失败: {exc} (status={exc.status_code})")
        if exc.body:
            print(f"  detail: {json.dumps(exc.body, ensure_ascii=False)[:300]}")
        return None
    if isinstance(resp, dict):
        run_id = resp.get("id") or resp.get("run_id")
        status = resp.get("status")
        print(f"  run_id={run_id} status={status}")
    return resp if isinstance(resp, dict) else None


# ---------------------------------------------------------------------------
# 4. 拉取事件流
# ---------------------------------------------------------------------------


def fetch_events(
    base_url: str,
    access_token: str | None,
    api_key: str | None,
    workflow_id: str,
    run_id: str,
    workspace_id: str | None,
    max_wait: float = 15.0,
) -> None:
    """轮询拉取运行事件流并打印。

    事件端点：GET /api/v1/workflows/{workflow_id}/runs/{run_id}/events
    返回事件列表，每条含 event_type 与 payload。
    """
    print("\n=== 3. 拉取运行事件流 ===")
    url = (
        f"{base_url.rstrip('/')}/api/v1/workflows/{workflow_id}"
        f"/runs/{run_id}/events"
    )
    headers = {
        "Accept": "application/json",
        "User-Agent": "workama-example-workflow/0.1.0",
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    elif api_key:
        headers["X-WorkAMA-API-Key"] = api_key
    if workspace_id:
        headers["X-Workspace-Id"] = workspace_id

    deadline = time.monotonic() + max_wait
    seen = 0
    while time.monotonic() < deadline:
        req = urllib.request.Request(url, method="GET", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            print(f"  [ERR] 事件接口返回 {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}")
            return
        except urllib.error.URLError as exc:
            print(f"  [ERR] 事件接口连接失败: {exc.reason}")
            return
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            print(f"  [WARN] 事件响应非 JSON: {raw[:120]!r}")
            return
        items = data.get("items") or data.get("events") or [] if isinstance(data, dict) else []
        for ev in items[seen:]:
            seen += 1
            etype = ev.get("event_type") or ev.get("type")
            payload = ev.get("payload") or {}
            node_id = payload.get("node_id")
            status = payload.get("status")
            print(f"  [{seen}] {etype} node={node_id} status={status}")
        # 判断是否终态
        terminal = any(
            (ev.get("event_type") or "").startswith("workflow.run.")
            and (ev.get("payload", {}) or {}).get("status") in {"succeeded", "failed", "cancelled"}
            for ev in items
        )
        if terminal or not items:
            # 终态或暂无事件：再多等一轮后退出
            if terminal:
                break
            time.sleep(0.8)
    print(f"  共收到 {seen} 条事件")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    base_url = os.environ.get("WORKAMA_BASE_URL", "http://localhost:20200")
    access_token = os.environ.get("WORKAMA_ACCESS_TOKEN")
    api_key = os.environ.get("WORKAMA_API_KEY")
    workspace_id = os.environ.get("WORKAMA_WORKSPACE_ID")

    if not access_token and not api_key:
        print("[WARN] 未设置 WORKAMA_ACCESS_TOKEN / WORKAMA_API_KEY，请求可能 401", file=sys.stderr)

    client = WorkAMAClient(base_url=base_url, api_key=api_key, access_token=access_token)

    wf_id = ensure_workflow(client, workspace_id)
    if not wf_id:
        print("[ERR] 没有可用工作流，示例终止")
        return 1

    run = trigger_run(client, wf_id, {"topic": "周报", "value": 200}, workspace_id)
    run_id = (run or {}).get("id") or (run or {}).get("run_id")
    if run_id:
        fetch_events(base_url, access_token, api_key, wf_id, run_id, workspace_id)

    print("\n=== 4. 运行结果 ===")
    print(json.dumps(run, ensure_ascii=False, indent=2)[:600] if run else "  （无运行结果）")

    print("\n[OK] workflow_demo 示例完成")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[中断]", file=sys.stderr)
        raise SystemExit(130)
