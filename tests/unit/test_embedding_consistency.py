"""embedding 一致性检测纯函数单元测试。"""

from mem_lake.embedding.consistency import (
    compute_signature,
    decide_embedding_change,
)


def test_compute_signature_from_provider_model():
    assert (
        compute_signature({"provider": "remote", "model": "text-embedding-3-small"})
        == "remote:text-embedding-3-small"
    )


def test_compute_signature_defaults_provider_to_local():
    assert (
        compute_signature({"model": "/models/Qwen3-Embedding-0.6B"})
        == "local:/models/Qwen3-Embedding-0.6B"
    )


def test_compute_signature_missing_model_placeholder():
    assert compute_signature({"provider": "remote"}) == "remote:"


def test_decide_baseline_when_no_history():
    assert decide_embedding_change(None, "local:x") == ("baseline", False)


def test_decide_unchanged_when_same():
    assert decide_embedding_change("local:x", "local:x") == ("unchanged", False)


def test_decide_changed_when_different():
    assert decide_embedding_change("local:a", "remote:b") == ("changed", True)
