from workama_platform.modules.rag_eval import (
    EvalCaseCreate,
    EvalRunCreate,
    _case_hash,
    _fuse_rows,
)


def test_eval_case_hash_is_deterministic_and_changes_with_expected_chunks():
    first = EvalCaseCreate(query="Where is the runbook?", expected_chunk_ids=["chk_1"])
    same = EvalCaseCreate(query="Where is the runbook?", expected_chunk_ids=["chk_1"])
    different = EvalCaseCreate(query="Where is the runbook?", expected_chunk_ids=["chk_2"])
    assert _case_hash(first) == _case_hash(same)
    assert _case_hash(first) != _case_hash(different)


def test_eval_fusion_deduplicates_chunks_and_keeps_rrf_order():
    keyword = [{"id": "chk_a", "content": "A"}, {"id": "chk_b", "content": "B"}]
    vector = [{"id": "chk_b", "content": "B"}, {"id": "chk_c", "content": "C"}]
    rows = _fuse_rows(keyword, vector, rrf_k=60, top_k=3)
    assert [row["id"] for row in rows] == ["chk_b", "chk_a", "chk_c"]


def test_eval_run_limits_are_validated():
    run = EvalRunCreate(eval_set_id="eval_1", top_k=5, candidate_k=20)
    assert run.top_k == 5
    assert EvalRunCreate(eval_set_id="eval_1", top_k=50, candidate_k=200).candidate_k == 200
