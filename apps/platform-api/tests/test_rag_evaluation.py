"""T-M3-003 RAG 评测集/标注回流单元测试。

测试覆盖：
- 评测集 CRUD 模型与校验
- 评测用例 CRUD 模型与去重哈希
- 评测运行启动/状态/结果模型
- 标注提交/获取/批量导入模型
- 指标计算（retrieval_recall / retrieval_precision / context_precision / answer_relevance）
- 聚合统计（mean / median / p90）
- RRF 融合算法

测试采用纯单元测试风格（与 test_rag_eval.py 一致），
不依赖数据库，专注于指标计算与模型校验。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from workama_platform.modules.knowledge_eval import (
    DEFAULT_METRICS,
    SUPPORTED_METRICS,
    AnnotationCreate,
    AnnotationImport,
    EvalCaseCreate,
    EvalCaseImport,
    EvalRunCreate,
    EvalSetCreate,
    _case_hash,
    _fuse_rows,
    _percentile,
    _tokenize,
    _validate_metrics,
    aggregate_metric,
    answer_relevance,
    compute_case_metrics,
    context_precision,
    retrieval_precision,
    retrieval_recall,
)


# ------------------------- 模型校验 -------------------------


def test_eval_set_create_defaults_match_design_spec():
    """评测集默认 metrics 与设计文档一致。"""
    body = EvalSetCreate(name="smoke", dataset_id="dts_1")
    assert body.name == "smoke"
    assert body.description == ""
    assert body.metrics == ["retrieval_recall", "retrieval_precision", "answer_relevance"]
    assert DEFAULT_METRICS == ["retrieval_recall", "retrieval_precision", "answer_relevance"]


def test_eval_set_create_rejects_empty_name():
    """评测集名称不能为空。"""
    with pytest.raises(Exception):
        EvalSetCreate(name="", dataset_id="dts_1")


def test_eval_case_create_defaults():
    """评测用例默认字段。"""
    case = EvalCaseCreate(question="如何重置密码？")
    assert case.question == "如何重置密码？"
    assert case.expected_answer == ""
    assert case.expected_chunks == []
    assert case.tags == []
    assert case.metadata == {}


def test_eval_case_import_validates_batch_size():
    """批量导入用例必须非空且不超过 500 条。"""
    # 单条用例可以
    body = EvalCaseImport(items=[EvalCaseCreate(question="Q1")])
    assert len(body.items) == 1
    # 0 条会失败
    with pytest.raises(Exception):
        EvalCaseImport(items=[])


def test_eval_run_create_defaults_align_with_retrieval_config():
    """评测运行默认配置与现有 retrieval config 默认值一致。"""
    body = EvalRunCreate()
    assert body.top_k == 5
    assert body.candidate_k == 20
    assert body.rrf_k == 60
    assert body.score_threshold == 0.0


def test_eval_run_create_rejects_invalid_top_k():
    """top_k 必须在 1-50 之间。"""
    with pytest.raises(Exception):
        EvalRunCreate(top_k=0)
    with pytest.raises(Exception):
        EvalRunCreate(top_k=51)


def test_annotation_create_validates_rating_range():
    """标注 rating 必须在 1-5 之间。"""
    AnnotationCreate(case_id="case_1", rating=1)
    AnnotationCreate(case_id="case_1", rating=5)
    with pytest.raises(Exception):
        AnnotationCreate(case_id="case_1", rating=0)
    with pytest.raises(Exception):
        AnnotationCreate(case_id="case_1", rating=6)


def test_annotation_import_validates_batch():
    """批量导入标注必须非空。"""
    AnnotationImport(items=[AnnotationCreate(case_id="case_1", rating=3)])
    with pytest.raises(Exception):
        AnnotationImport(items=[])


# ------------------------- 指标计算 -------------------------


def test_retrieval_recall_full_hit_returns_one():
    """所有期望文档都被检索到时，召回率应为 1.0。"""
    assert retrieval_recall(["chk_a", "chk_b"], ["chk_a", "chk_b"]) == 1.0


def test_retrieval_recall_partial_hit():
    """部分命中的召回率。"""
    # 2 个期望，命中 1 个 -> 0.5
    assert retrieval_recall(["chk_a", "chk_c"], ["chk_a", "chk_b"]) == pytest.approx(0.5)


def test_retrieval_recall_no_hit_returns_zero():
    """没有命中时召回率为 0。"""
    assert retrieval_recall(["chk_x", "chk_y"], ["chk_a", "chk_b"]) == 0.0


def test_retrieval_recall_empty_expected_returns_one():
    """没有期望文档时返回 1.0（视为完美召回）。"""
    assert retrieval_recall(["chk_a"], []) == 1.0
    assert retrieval_recall([], []) == 1.0


def test_retrieval_recall_empty_retrieved_returns_zero_when_expected_present():
    """有期望但无检索结果时返回 0。"""
    assert retrieval_recall([], ["chk_a"]) == 0.0


def test_retrieval_precision_full_hit_returns_one():
    """检索结果全部相关时精度为 1.0。"""
    assert retrieval_precision(["chk_a", "chk_b"], ["chk_a", "chk_b"]) == 1.0


def test_retrieval_precision_partial_hit():
    """部分相关的精度。"""
    # 检索 4 个，相关 2 个 -> 0.5
    assert retrieval_precision(["chk_a", "chk_b", "chk_x", "chk_y"], ["chk_a", "chk_b"]) == pytest.approx(0.5)


def test_retrieval_precision_no_overlap_returns_zero():
    """无重叠时精度为 0。"""
    assert retrieval_precision(["chk_x", "chk_y"], ["chk_a", "chk_b"]) == 0.0


def test_retrieval_precision_empty_retrieved_returns_zero():
    """无检索结果时精度为 0。"""
    assert retrieval_precision([], ["chk_a"]) == 0.0


def test_retrieval_precision_empty_expected_returns_zero():
    """无期望文档时精度为 0（避免误报）。"""
    assert retrieval_precision(["chk_a"], []) == 0.0


def test_context_precision_perfect_order_returns_one():
    """相关文档全部排在最前时，上下文精度为 1.0。"""
    # 期望 chk_a, chk_b；检索顺序 chk_a, chk_b, chk_x
    assert context_precision(["chk_a", "chk_b", "chk_x"], ["chk_a", "chk_b"]) == pytest.approx(1.0)


def test_context_precision_lower_when_relevant_later():
    """相关文档越靠后，上下文精度越低。"""
    perfect = context_precision(["chk_a", "chk_b"], ["chk_a", "chk_b"])
    late = context_precision(["chk_x", "chk_a", "chk_b"], ["chk_a", "chk_b"])
    assert perfect > late
    assert perfect == pytest.approx(1.0)
    assert 0 < late < 1.0


def test_context_precision_no_overlap_returns_zero():
    """无重叠时上下文精度为 0。"""
    assert context_precision(["chk_x", "chk_y"], ["chk_a", "chk_b"]) == 0.0


def test_answer_relevance_identical_text_returns_one():
    """完全相同的文本相关性为 1.0。"""
    text = "密码重置需要通过邮箱验证"
    assert answer_relevance(text, text) == pytest.approx(1.0)


def test_answer_relevance_no_overlap_returns_zero():
    """无 token 重叠时相关性为 0。"""
    assert answer_relevance("apple banana", "cherry date") == 0.0


def test_answer_relevance_empty_expected_returns_one():
    """无期望答案时相关性为 1.0。"""
    assert answer_relevance("anything", "") == 1.0


def test_answer_relevance_empty_generated_returns_zero_when_expected_present():
    """有期望但无生成答案时相关性为 0。"""
    assert answer_relevance("", "expected answer") == 0.0


def test_answer_relevance_partial_overlap_returns_f1():
    """部分重叠时返回 F1 分数。"""
    # 生成 "apple banana cherry"，期望 "banana cherry date"
    # 重叠 = {banana, cherry}, gen={apple, banana, cherry}, exp={banana, cherry, date}
    # precision = 2/3, recall = 2/3, F1 = 2/3
    score = answer_relevance("apple banana cherry", "banana cherry date")
    assert score == pytest.approx(2 / 3, rel=1e-3)


def test_tokenize_filters_single_character_noise():
    """分词器过滤单字符噪音。"""
    tokens = _tokenize("a bb ccc")
    assert tokens == {"bb", "ccc"}


def test_tokenize_handles_chinese_via_unicode_word_boundary():
    """分词器对中文按非字母数字切分。"""
    tokens = _tokenize("密码 重置 邮箱")
    assert "密码" in tokens
    assert "重置" in tokens
    assert "邮箱" in tokens


# ------------------------- 指标聚合 -------------------------


def test_aggregate_metric_empty_returns_zeros():
    """空列表聚合返回 0。"""
    result = aggregate_metric([])
    assert result == {"mean": 0.0, "median": 0.0, "p90": 0.0, "count": 0}


def test_aggregate_metric_single_value():
    """单值聚合：mean=median=p90=value。"""
    result = aggregate_metric([0.7])
    assert result["mean"] == pytest.approx(0.7)
    assert result["median"] == pytest.approx(0.7)
    assert result["p90"] == pytest.approx(0.7)
    assert result["count"] == 1


def test_aggregate_metric_multiple_values():
    """多值聚合：平均分、中位数、P90 各自正确。"""
    values = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    result = aggregate_metric(values)
    assert result["mean"] == pytest.approx(0.55)
    assert result["median"] == pytest.approx(0.55)  # (0.5+0.6)/2
    assert result["p90"] == pytest.approx(0.91)  # 0.9 + (1.0-0.9)*0.9
    assert result["count"] == 10


def test_percentile_handles_extremes():
    """百分位边界：p=0 返回最小值，p=1 返回最大值。"""
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert _percentile(values, 0.0) == pytest.approx(1.0)
    assert _percentile(values, 1.0) == pytest.approx(5.0)
    assert _percentile(values, 0.5) == pytest.approx(3.0)
    assert _percentile([], 0.5) == 0.0


# ------------------------- 综合指标计算 -------------------------


def test_compute_case_metrics_returns_only_requested_metrics():
    """仅计算请求的指标。"""
    metrics = compute_case_metrics(
        retrieved_ids=["chk_a", "chk_b"],
        expected_ids=["chk_a"],
        generated_answer="hello world",
        expected_answer="hello",
        metrics=["retrieval_recall"],
    )
    assert set(metrics.keys()) == {"retrieval_recall"}
    assert metrics["retrieval_recall"] == pytest.approx(1.0)


def test_compute_case_metrics_all_four_metrics():
    """计算全部 4 个指标。"""
    metrics = compute_case_metrics(
        retrieved_ids=["chk_a", "chk_b", "chk_c"],
        expected_ids=["chk_a", "chk_b"],
        generated_answer="hello world foo",
        expected_answer="hello world",
        metrics=["retrieval_recall", "retrieval_precision", "context_precision", "answer_relevance"],
    )
    assert set(metrics.keys()) == {"retrieval_recall", "retrieval_precision", "context_precision", "answer_relevance"}
    assert metrics["retrieval_recall"] == pytest.approx(1.0)
    assert metrics["retrieval_precision"] == pytest.approx(2 / 3)
    assert 0 < metrics["context_precision"] <= 1.0
    assert 0 < metrics["answer_relevance"] <= 1.0


def test_compute_case_metrics_default_metrics_when_none_provided():
    """未指定 metrics 时使用默认值。"""
    metrics = compute_case_metrics(
        retrieved_ids=["chk_a"],
        expected_ids=["chk_a"],
        generated_answer="",
        expected_answer="",
    )
    assert set(metrics.keys()) == set(DEFAULT_METRICS)


# ------------------------- 模型校验辅助函数 -------------------------


def test_validate_metrics_filters_invalid():
    """非法 metric 触发 422。"""
    with pytest.raises(HTTPException) as exc:
        _validate_metrics(["retrieval_recall", "bogus_metric"])
    assert exc.value.status_code == 422


def test_validate_metrics_returns_cleaned_list():
    """合法 metric 列表原样返回（剔除空字符串）。"""
    cleaned = _validate_metrics(["retrieval_recall", " ", "retrieval_precision"])
    assert cleaned == ["retrieval_recall", "retrieval_precision"]


def test_validate_metrics_empty_input_returns_defaults():
    """空 metrics 列表回退到默认值。"""
    cleaned = _validate_metrics([])
    assert cleaned == DEFAULT_METRICS


def test_supported_metrics_contains_all_design_spec_metrics():
    """支持的指标集合覆盖设计文档定义的 4 个。"""
    assert SUPPORTED_METRICS == {
        "retrieval_recall",
        "retrieval_precision",
        "answer_relevance",
        "context_precision",
    }


# ------------------------- 用例去重哈希 -------------------------


def test_case_hash_is_deterministic():
    """相同用例内容生成相同哈希。"""
    case = EvalCaseCreate(question="Q1", expected_chunks=["chk_a"])
    same = EvalCaseCreate(question="Q1", expected_chunks=["chk_a"])
    assert _case_hash(case) == _case_hash(same)


def test_case_hash_changes_with_expected_chunks():
    """期望 chunks 不同则哈希不同。"""
    first = EvalCaseCreate(question="Q1", expected_chunks=["chk_a"])
    different = EvalCaseCreate(question="Q1", expected_chunks=["chk_b"])
    assert _case_hash(first) != _case_hash(different)


def test_case_hash_changes_with_metadata():
    """metadata 不同则哈希不同。"""
    base = EvalCaseCreate(question="Q1", metadata={})
    tagged = EvalCaseCreate(question="Q1", metadata={"source": "import"})
    assert _case_hash(base) != _case_hash(tagged)


# ------------------------- RRF 融合算法 -------------------------


def test_fuse_rows_deduplicates_and_orders_by_rrf_score():
    """融合去重并按 RRF 分数排序。"""
    keyword = [{"id": "chk_a", "content": "A"}, {"id": "chk_b", "content": "B"}]
    vector = [{"id": "chk_b", "content": "B"}, {"id": "chk_c", "content": "C"}]
    rows = _fuse_rows(keyword, vector, rrf_k=60, top_k=3)
    # chk_b 在两个列表中都出现，得分最高
    assert [row["id"] for row in rows] == ["chk_b", "chk_a", "chk_c"]


def test_fuse_rows_respects_top_k_limit():
    """top_k 限制返回数量。"""
    keyword = [{"id": f"chk_{i}"} for i in range(5)]
    vector = [{"id": f"chk_{i}"} for i in range(5, 10)]
    rows = _fuse_rows(keyword, vector, rrf_k=60, top_k=3)
    assert len(rows) == 3


def test_fuse_rows_empty_inputs_returns_empty():
    """空输入返回空列表。"""
    assert _fuse_rows([], [], rrf_k=60, top_k=5) == []


# ------------------------- mock 检索结果综合验证 -------------------------


def test_metrics_end_to_end_with_mocked_retrieval_results():
    """模拟检索结果，端到端验证 recall/precision/context_precision/answer_relevance。"""
    # 场景：用户问"如何重置密码"
    # 期望命中的 chunks: chk_pwd_1, chk_pwd_2
    # 检索返回: chk_pwd_1, chk_email, chk_pwd_2, chk_other
    retrieved = ["chk_pwd_1", "chk_email", "chk_pwd_2", "chk_other"]
    expected = ["chk_pwd_1", "chk_pwd_2"]

    # 检索召回率：2/2 = 1.0
    assert retrieval_recall(retrieved, expected) == pytest.approx(1.0)
    # 检索精度：2/4 = 0.5
    assert retrieval_precision(retrieved, expected) == pytest.approx(0.5)
    # 上下文精度：位置 1 + 位置 3 -> (1/1 + 1/3) / (1/1 + 1/2) = (4/3) / (3/2) = 8/9
    cp = context_precision(retrieved, expected)
    expected_cp = (1.0 + 1.0 / 3) / (1.0 + 1.0 / 2)
    assert cp == pytest.approx(expected_cp)
    # 答案相关性（分词器按非字母数字切分，需要空格/标点分隔才能形成多 token）
    gen = "user can reset password via email link"
    exp = "reset password requires email verification"
    ar = answer_relevance(gen, exp)
    # 共享 token: reset, password, email -> 应有显著重叠
    assert 0 < ar <= 1.0


def test_metrics_aggregation_for_multiple_cases():
    """多用例指标聚合。"""
    # 模拟 3 个用例的检索召回率
    recall_values = [1.0, 0.5, 0.0]
    agg = aggregate_metric(recall_values)
    assert agg["mean"] == pytest.approx(0.5)
    assert agg["median"] == pytest.approx(0.5)
    assert agg["count"] == 3
    # P90 应在 0.5 和 1.0 之间
    assert 0.5 <= agg["p90"] <= 1.0
