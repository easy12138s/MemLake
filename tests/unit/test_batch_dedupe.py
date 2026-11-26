"""批内需求查重测试（无 DB）。"""

import uuid

import pytest

from mem_lake.gateway.tools._batch_dedupe import (
    DedupeEntry,
    find_duplicates,
    find_exact_duplicates,
    normalize_fingerprint,
)


def _entry(ref: str, title: str, content: str = "内容", project_id=None, external_id=None):
    return DedupeEntry(
        client_ref=ref,
        project_id=project_id,
        title=title,
        content=content,
        external_id=external_id,
    )


def test_normalize_fingerprint_collapses_unicode_and_whitespace():
    assert normalize_fingerprint("  Hello　World ", "A\n B") == normalize_fingerprint(
        "hello world", "a b"
    )


def test_exact_duplicate_across_projects_is_reported():
    entries = [
        _entry("A", "医保结算", project_id=uuid.uuid4()),
        _entry("B", "医保结算", project_id=uuid.uuid4()),
    ]

    dup = find_exact_duplicates(entries)

    assert "A" in dup and "B" in dup
    assert any("B" in r for r in dup["A"])
    assert any("重复" in r for r in dup["A"])


def test_external_id_duplicate_is_reported_even_if_content_differs():
    entries = [
        _entry("A", "需求一", external_id="JIRA-1"),
        _entry("B", "需求二", external_id="JIRA-1"),
    ]

    dup = find_exact_duplicates(entries)

    assert any("external_id" in r for r in dup["A"])


class _FakeEmbedding:
    def __init__(self, vectors):
        self.vectors = vectors
        self.calls = 0

    async def embed(self, texts, prompt=None, prompt_name=None):
        self.calls += 1
        return self.vectors


@pytest.mark.asyncio
async def test_near_duplicate_uses_one_embedding_call():
    entries = [_entry("A", "医保结算"), _entry("B", "医保结算流程")]
    fake = _FakeEmbedding([[1.0, 0.0], [0.99, 0.1]])

    result = await find_duplicates(entries, embedding_client=fake, threshold=0.85)

    assert fake.calls == 1
    assert result.degraded is False
    assert any("高度相似" in r for r in result.duplicates["A"])


@pytest.mark.asyncio
async def test_embedding_failure_degrades_to_exact_only():
    entries = [_entry("A", "同一需求"), _entry("B", "同一需求")]

    class _Broken:
        async def embed(self, texts, prompt=None, prompt_name=None):
            raise RuntimeError("embedding down")

    result = await find_duplicates(entries, embedding_client=_Broken(), threshold=0.85)

    assert result.degraded is True
    assert "A" in result.duplicates
