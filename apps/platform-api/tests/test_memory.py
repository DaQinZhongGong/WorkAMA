from workama_platform.modules.memory import (
    SEMANTIC_DIMENSIONS,
    cosine_similarity,
    memory_matches,
    normalize_memory_key,
    rank_memories,
    semantic_embedding,
)


def test_memory_keys_are_canonicalized_for_idempotent_profile_updates():
    assert normalize_memory_key("  Preferred  language  ") == "preferred language"
    assert normalize_memory_key(" ") == ""


def test_memory_search_is_case_insensitive_and_only_matches_key_or_content():
    row = {"memory_key": "preferred language", "content": "The user prefers Python."}
    assert memory_matches(row, "PYTHON")
    assert memory_matches(row, "preferred")
    assert not memory_matches(row, "billing")
    assert memory_matches(row, "")


def test_local_semantic_embedding_is_stable_and_normalized():
    first = semantic_embedding("preferred language", "The user prefers Python")
    second = semantic_embedding("preferred language", "The user prefers Python")
    assert first == second
    assert len(first) == SEMANTIC_DIMENSIONS
    assert cosine_similarity(first, first) == 1.0


def test_hybrid_memory_ranking_can_recall_related_content_without_exact_phrase():
    rows = [
        {"id": "m1", "memory_key": "coding language", "content": "The user prefers Python."},
        {"id": "m2", "memory_key": "billing", "content": "The user has a monthly plan."},
    ]
    ranked = rank_memories(rows, "Python", mode="hybrid", limit=1)
    assert ranked[0]["id"] == "m1"
    assert ranked[0]["semantic_score"] > 0


def test_semantic_ranking_is_fail_closed_for_malformed_stored_vectors():
    rows = [{"id": "m1", "memory_key": "profile", "content": "hello", "semantic_embedding": ["bad"]}]
    ranked = rank_memories(rows, "unrelated", mode="semantic", limit=10)
    assert ranked == []
