"""WorkAMA Python SDK 快速开始示例。

完整演示：登录 -> 列出 Agent -> 对话 -> 创建记忆 -> 检索记忆 -> 搜索知识 -> 执行工作流。

运行方式：
    cd examples/python-quickstart
    pip install -e ../../packages/sdk-python
    python main.py

环境变量（可选）：
    WORKAMA_BASE_URL   平台 API 基地址，默认 http://localhost:20200
    WORKAMA_API_KEY    API Key（与 WORKAMA_ACCESS_TOKEN 二选一）
    WORKAMA_ACCESS_TOKEN  Bearer Token（优先级高于 API Key）
    WORKAMA_AGENT_ID   演示对话使用的 Agent ID
"""

from __future__ import annotations

import os
import sys

from workama_sdk import (
    AuthenticationError,
    NotFoundError,
    RateLimitError,
    WorkAMAClient,
    WorkAMAError,
)


def main() -> int:
    base_url = os.environ.get("WORKAMA_BASE_URL", "http://localhost:20200")
    api_key = os.environ.get("WORKAMA_API_KEY")
    access_token = os.environ.get("WORKAMA_ACCESS_TOKEN")
    agent_id = os.environ.get("WORKAMA_AGENT_ID", "agent_demo")

    if not api_key and not access_token:
        print("[WARN] 未设置 WORKAMA_API_KEY / WORKAMA_ACCESS_TOKEN，后续请求可能 401", file=sys.stderr)

    client = WorkAMAClient(
        base_url=base_url,
        api_key=api_key,
        access_token=access_token,
        timeout=30.0,
    )

    try:
        # 1) 列出 Agent
        print("\n=== 1. list_agents ===")
        agents = client.list_agents(limit=20)
        print(f"agent 数量: {len(agents.get('items', []))}")
        for a in agents.get("items", [])[:3]:
            print(f"  - {a}")

        # 2) 与 Agent 对话
        print("\n=== 2. chat ===")
        chat = client.chat(agent_id, "你好，请用一句话介绍 WorkAMA", session_id=None)
        print(f"reply: {chat.get('message', '')[:200]}")

        # 3) 创建记忆
        print("\n=== 3. create_memory ===")
        mem = client.create_memory(
            content="用户喜欢简洁的回复，避免冗长输出",
            metadata={"category": "preference", "source": "quickstart"},
            importance=4,
        )
        print(f"created memory: {mem}")

        # 4) 检索记忆
        print("\n=== 4. recall_memory ===")
        recall = client.recall_memory(query="用户偏好", limit=3)
        for item in recall.get("items", [])[:3]:
            print(f"  - score={item.get('score'):.3f} content={item.get('content', '')[:60]}")

        # 5) 搜索知识库
        print("\n=== 5. search_knowledge ===")
        results = client.search_knowledge(query="产品定价", dataset_id=None, limit=5)
        for hit in results.get("items", [])[:3]:
            print(f"  - score={hit.get('score')} content={hit.get('content', '')[:60]}")

        # 6) 列出工作流并执行
        print("\n=== 6. list_workflows & run_workflow ===")
        workflows = client.list_workflows(limit=10)
        wf_items = workflows.get("items", [])
        print(f"workflow 数量: {len(wf_items)}")
        if wf_items:
            wf_id = wf_items[0].get("id") or "wf_demo"
            run = client.run_workflow(wf_id, inputs={"topic": "本周项目进展"})
            print(f"run: {run}")

        print("\n[OK] quickstart 完成")
        return 0

    except AuthenticationError as e:
        print(f"[ERR] 鉴权失败: {e} (body={e.body})", file=sys.stderr)
        return 2
    except NotFoundError as e:
        print(f"[ERR] 资源不存在: {e} (body={e.body})", file=sys.stderr)
        return 3
    except RateLimitError as e:
        print(f"[ERR] 被限流: {e} (body={e.body})", file=sys.stderr)
        return 4
    except WorkAMAError as e:
        print(f"[ERR] SDK 错误: {e} (status={e.status_code}, body={e.body})", file=sys.stderr)
        return 5
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
