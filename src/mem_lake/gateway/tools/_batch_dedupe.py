"""批内需求查重：exact 指纹 / external_id + 可选向量近重复。

设计（方案 A）：同一 `publish_requirements` 调用内、system 级两两比较，
不按 project 分组；`external_id` 非空且相同即 exact duplicate，跨 project
同样算重复。向量近重复需要 embedding 客户端，失败时降级为 exact-only。
"""

from __future__ import annotations

import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any, Sequence

from mem_lake.config import get_settings


@dataclass(frozen=True)
class DedupeEntry:
    """批内单条需求的最小比较单元。"""

    client_ref: str
    project_id: uuid.UUID | None
    title: str
    content: str
    external_id: str | None = None


@dataclass
class DedupeResult:
    """查重结果：client_ref -> 命中的冲突描述列表。"""

    duplicates: dict[str, list[str]] = field(default_factory=dict)
    degraded: bool = False


def normalize_fingerprint(title: str, content: str) -> str:
    """归一化标题+正文用于 exact 判重（NFKC、casefold、空白折叠）。"""
    text = unicodedata.normalize("NFKC", f"{title}\n{content}")
    return " ".join(text.casefold().split())


def _project_label(project_id: uuid.UUID | None) -> str:
    return f"project={project_id}" if project_id is not None else "悬浮"


def _link(
    reasons: dict[str, set[str]],
    group: Sequence[DedupeEntry],
    template: str,
) -> None:
    for entry in group:
        for other in group:
            if entry.client_ref == other.client_ref:
                continue
            reasons.setdefault(entry.client_ref, set()).add(
                template.format(other=other.client_ref, project=_project_label(other.project_id))
            )


def find_exact_duplicates(entries: Sequence[DedupeEntry]) -> dict[str, list[str]]:
    """按归一化指纹与 external_id 找出批内重复，返回冲突描述。"""
    by_fingerprint: dict[str, list[DedupeEntry]] = {}
    by_external_id: dict[str, list[DedupeEntry]] = {}
    for entry in entries:
        by_fingerprint.setdefault(
            normalize_fingerprint(entry.title, entry.content), []
        ).append(entry)
        if entry.external_id:
            by_external_id.setdefault(entry.external_id, []).append(entry)

    reasons: dict[str, set[str]] = {}
    for group in by_fingerprint.values():
        if len(group) > 1:
            _link(reasons, group, "与 {other}（{project}）内容重复")
    for group in by_external_id.values():
        if len(group) > 1:
            _link(reasons, group, "与 {other}（{project}）external_id 重复")
    return {ref: sorted(items) for ref, items in reasons.items()}


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    return float(sum(a * b for a, b in zip(left, right)))


async def find_duplicates(
    entries: Sequence[DedupeEntry],
    *,
    embedding_client: Any | None = None,
    threshold: float | None = None,
) -> DedupeResult:
    """合并 exact 与向量近重复检测；embedding 不可用时降级为 exact-only。"""
    if threshold is None:
        threshold = get_settings().CONFLICT_SIMILARITY_THRESHOLD

    duplicates: dict[str, set[str]] = {
        ref: set(items) for ref, items in find_exact_duplicates(entries).items()
    }
    degraded = False

    if embedding_client is not None and len(entries) > 1:
        try:
            vectors = await embedding_client.embed(
                [f"{e.title}\n{e.content}" for e in entries], prompt_name="query"
            )
            for i, left in enumerate(entries):
                for j in range(i + 1, len(entries)):
                    score = _cosine(vectors[i], vectors[j])
                    if score < threshold:
                        continue
                    right = entries[j]
                    duplicates.setdefault(left.client_ref, set()).add(
                        f"与 {right.client_ref}（{_project_label(right.project_id)}）"
                        f"高度相似（score={score:.2f}）"
                    )
                    duplicates.setdefault(right.client_ref, set()).add(
                        f"与 {left.client_ref}（{_project_label(left.project_id)}）"
                        f"高度相似（score={score:.2f}）"
                    )
        except Exception:  # noqa: BLE001 - 查重增强失败不阻断提交，降级 exact
            degraded = True

    return DedupeResult(
        duplicates={ref: sorted(items) for ref, items in duplicates.items()},
        degraded=degraded,
    )
