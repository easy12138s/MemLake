"""embedding_remote 转发纯逻辑单元测试：请求构造与响应解析（无需真实 embedding/外部 API）。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy"))

from embedding_remote import build_embed_request, parse_embeddings_response  # noqa: E402


def test_build_embed_request_shape():
    """请求体含 model / input（数组）/ dimensions。"""
    body = build_embed_request("text-embedding-3-small", ["a", "b"], 1024)
    assert body["model"] == "text-embedding-3-small"
    assert body["input"] == ["a", "b"]
    assert body["dimensions"] == 1024


def test_parse_embeddings_response_reorders_by_index():
    """API 返回顺序与输入不同（index 乱序）时，应按 index 还原为输入顺序。"""
    payload = {
        "data": [
            {"index": 1, "embedding": [0.2, 0.2]},
            {"index": 0, "embedding": [0.1, 0.1]},
        ]
    }
    result = parse_embeddings_response(payload, expected_count=2, expected_dim=2)
    assert result == [[0.1, 0.1], [0.2, 0.2]]


def test_parse_embeddings_response_missing_data():
    with pytest.raises(ValueError, match="data"):
        parse_embeddings_response({}, expected_count=1, expected_dim=2)


def test_parse_embeddings_response_count_mismatch():
    payload = {"data": [{"index": 0, "embedding": [0.1, 0.1]}]}
    with pytest.raises(ValueError, match="数量不符"):
        parse_embeddings_response(payload, expected_count=2, expected_dim=2)


def test_parse_embeddings_response_dimension_mismatch():
    payload = {"data": [{"index": 0, "embedding": [0.1]}]}
    with pytest.raises(ValueError, match="维度不符"):
        parse_embeddings_response(payload, expected_count=1, expected_dim=2)


def test_parse_embeddings_response_incomplete_index():
    """data 数量对上但 index 重复/缺失时，应报 index 不完整。"""
    payload = {
        "data": [
            {"index": 0, "embedding": [0.1, 0.1]},
            {"index": 0, "embedding": [0.2, 0.2]},
        ]
    }
    with pytest.raises(ValueError, match="index 不完整"):
        parse_embeddings_response(payload, expected_count=2, expected_dim=2)
