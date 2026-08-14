from __future__ import annotations

import pytest

from workama_platform.modules.knowledge import (
    RagProcessingError,
    _clean_text,
    _extract_text,
    _normalize_retrieval_config,
    _normalize_vector,
    _split_text,
    _vector_literal,
)


def test_text_parser_cleaner_and_heading_chunks_preserve_source_metadata():
    extracted = _extract_text(
        b"# Runbook\n\nThis platform routes documents through the gateway.\n\nThe index records source metadata for citations.\n",
        "text/markdown",
        "runbook.md",
    )
    cleaned = _clean_text(extracted[0][0])
    chunks = _split_text(cleaned, extracted[0][1])

    assert chunks
    assert all(item[1]["source_name"] == "runbook.md" for item in chunks)
    assert any(item[1].get("title_path") == ["Runbook"] for item in chunks)


def test_embedding_normalization_and_pgvector_literal_are_stable():
    vector = _normalize_vector([3, 4])

    assert vector == pytest.approx([0.6, 0.8])
    assert _vector_literal(vector).startswith("[")
    assert _vector_literal(vector).endswith("]")


def test_invalid_embeddings_and_invalid_retrieval_configs_are_rejected():
    with pytest.raises(RagProcessingError):
        _normalize_vector([0, 0])

    with pytest.raises(Exception):
        _normalize_retrieval_config({"top_k": 20, "candidate_k": 5})
