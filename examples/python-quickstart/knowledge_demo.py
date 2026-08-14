"""WorkAMA 知识库 RAG 示例（P2 第三方集成）。

演示：
1. 创建知识库（指定 embedding 模型与分块策略）
2. 批量上传文档（自动分块 + embedding）
3. 查询检索，显示命中条目与相关性分数（similarity）

运行方式：
    cd examples/python-quickstart
    pip install -e ../../packages/sdk-python
    python knowledge_demo.py

环境变量：
    WORKAMA_BASE_URL       平台 API 基地址，默认 http://localhost:20200
    WORKAMA_ACCESS_TOKEN   Bearer Token（优先）
    WORKAMA_API_KEY        API Key（与 token 二选一）
    WORKAMA_WORKSPACE_ID   可选，工作空间隔离标识
    WORKAMA_KB_ID          可选，复用已有知识库；否则示例会新建
"""

from __future__ import annotations

import os
import sys
import time

from workama_sdk import (
    ForbiddenError,
    WorkAMAClient,
    WorkAMAError,
)


# 演示用文档集：模拟企业内部 FAQ / 产品手册片段
SAMPLE_DOCS = [
    {
        "title": "WorkAMA 产品定价",
        "content": (
            "WorkAMA 提供三档套餐：Starter 每月 99 元，包含 10 万 token 与 1 个工作空间；"
            "Pro 每月 499 元，包含 100 万 token、5 个工作空间与 RAG 知识库；"
            "Enterprise 按年签约，提供私有化部署、SSO 与专属支持。"
        ),
        "source_type": "manual",
        "metadata": {"category": "pricing", "lang": "zh"},
    },
    {
        "title": "RAG 知识库使用指南",
        "content": (
            "知识库支持文本、Markdown 与 PDF 文档，写入后自动分块并生成 embedding。"
            "检索时基于余弦相似度返回 top_k 条目，similarity 越接近 1 表示越相关。"
            "可通过 chunk_size 与 chunk_overlap 调节分块粒度。"
        ),
        "source_type": "manual",
        "metadata": {"category": "rag", "lang": "zh"},
    },
    {
        "title": "工作流编排能力",
        "content": (
            "WorkAMA 工作流支持 HTTP 节点、Code 节点、Branch 条件分支与 LLM 节点，"
            "可通过 graph 定义 DAG，运行后产生事件流供订阅。支持幂等键与 dry_run 预演。"
        ),
        "source_type": "manual",
        "metadata": {"category": "workflow", "lang": "zh"},
    },
]


# ---------------------------------------------------------------------------
# 1. 创建知识库
# ---------------------------------------------------------------------------


def ensure_knowledge_base(client: WorkAMAClient, workspace_id: str | None) -> str | None:
    """返回一个可用 kb_id；优先复用环境变量，否则新建。"""
    existing = os.environ.get("WORKAMA_KB_ID")
    if existing:
        print(f"[INFO] 复用环境变量指定的知识库: {existing}")
        return existing

    print("\n=== 1. 创建知识库 ===")
    payload = {
        "name": "sdk-knowledge-demo",
        "description": "Python SDK knowledge_demo 临时知识库",
        "kind": "text",
        "embedding_model": "text-embedding-3-small",
        "embedding_dimensions": 1536,
        "chunk_size": 256,
        "chunk_overlap": 32,
    }
    try:
        resp = client.create_knowledge_base(payload, workspace_id=workspace_id)
    except ForbiddenError as exc:
        print(f"[ERR] 无权创建知识库（403）: {exc}")
        return None
    except WorkAMAError as exc:
        print(f"[ERR] 创建知识库失败: {exc}")
        return None
    kb_id = resp.get("id") if isinstance(resp, dict) else None
    print(f"  created kb_id={kb_id}")
    return kb_id


# ---------------------------------------------------------------------------
# 2. 批量上传文档
# ---------------------------------------------------------------------------


def batch_ingest(
    client: WorkAMAClient,
    kb_id: str,
    docs: list[dict],
    workspace_id: str | None,
) -> list[dict]:
    """批量写入文档，返回每条文档的服务端响应。"""
    print(f"\n=== 2. 批量上传文档（{len(docs)} 条）===")
    results: list[dict] = []
    for idx, doc in enumerate(docs, 1):
        try:
            resp = client.ingest_document(
                kb_id,
                content=doc["content"],
                metadata={
                    "title": doc["title"],
                    "source_type": doc.get("source_type", "manual"),
                    **doc.get("metadata", {}),
                },
                workspace_id=workspace_id,
            )
            doc_id = resp.get("id") if isinstance(resp, dict) else None
            chunk_count = resp.get("chunk_count") if isinstance(resp, dict) else None
            status = resp.get("status") if isinstance(resp, dict) else None
            print(f"  [{idx}/{len(docs)}] doc_id={doc_id} chunks={chunk_count} status={status}")
            results.append(resp if isinstance(resp, dict) else {})
            # 写入后 embedding 需要少量时间落库，简单节流避免突发
            time.sleep(0.1)
        except WorkAMAError as exc:
            print(f"  [{idx}/{len(docs)}] [ERR] {exc} (status={exc.status_code})")
            results.append({"error": str(exc)})
    return results


# ---------------------------------------------------------------------------
# 3. 查询检索 + 相关性分数
# ---------------------------------------------------------------------------


def query_and_print(
    client: WorkAMAClient,
    kb_id: str,
    query: str,
    top_k: int,
    workspace_id: str | None,
) -> None:
    """执行 RAG 查询并打印命中条目与 similarity 分数。"""
    print(f"\n=== 3. 查询检索 ===\n  query={query!r} top_k={top_k}")
    try:
        resp = client.query_knowledge(kb_id, query, top_k=top_k, workspace_id=workspace_id)
    except WorkAMAError as exc:
        print(f"  [ERR] 查询失败: {exc} (status={exc.status_code})")
        return

    items = []
    if isinstance(resp, dict):
        # 平台返回 results / data 两个字段，均含命中列表
        items = resp.get("results") or resp.get("data") or []
    print(f"  命中 {len(items)} 条：")
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            continue
        similarity = item.get("similarity")
        score = item.get("score", similarity)
        content = str(item.get("content", ""))[:80]
        score_str = f"{score:.4f}" if isinstance(score, (int, float)) else "N/A"
        print(f"  [{i}] score={score_str} content={content}")
    if not items:
        print("  （无命中；可能是文档仍在 embedding 中，稍后重试即可）")


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

    kb_id = ensure_knowledge_base(client, workspace_id)
    if not kb_id:
        print("[ERR] 没有可用知识库，示例终止")
        return 1

    batch_ingest(client, kb_id, SAMPLE_DOCS, workspace_id)

    # 多个查询演示不同相关性
    for q in ["Pro 套餐多少钱？", "如何调节分块粒度？", "工作流支持哪些节点？"]:
        query_and_print(client, kb_id, q, top_k=3, workspace_id=workspace_id)

    print("\n[OK] knowledge_demo 示例完成")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[中断]", file=sys.stderr)
        raise SystemExit(130)
