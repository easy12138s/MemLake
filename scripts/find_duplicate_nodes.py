"""存量重复节点检测脚本（只读运维工具，批次五 P1-3）。

背景（09-29 报告 P1-3）：跨版本语料逐字重复（如 TODO-P0 与 v2.15优化 doc 下
正文完全相同的需求各存一份）稀释检索 Precision；且历史宽松模式查重候选域
单 project 隔离（批次四已修复增量侧），存量重复需一次性清点。

本脚本只做检测与清点（不删不改）：
1. 精确重复：同类型 + (title, content) 归一化后逐字相同 → 分组列出
2. 语义重复（可选 --semantic）：同类型 + 关键标识字段相同（L2 口径）+ 向量
   余弦 ≥ 阈值，按 scripts/calibrate_conflict_threshold.py 的 doc-doc 口径

处理建议（人工确认后执行）：
- 用 update_node 把重复节点改 status=archived（软删）
- 或 admin 用 manage_system/知识治理工具维护 duplicates 边

用法：
    python -m scripts.find_duplicate_nodes [--semantic] [--threshold 0.95]
        [--node-type Requirement] [--limit 20]
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict

from sqlalchemy import select

from mem_lake.db.session import AsyncSessionLocal
from mem_lake.knowledge.models import KnowledgeNode, NodeEmbedding

# 与 approval/conflict.py KEY_IDENTITY_FIELDS 对齐（L2/L0 口径）
KEY_IDENTITY_FIELDS: dict[str, list[str]] = {
    "CodeSnippet": ["name", "file_path"],
    "Requirement": [],
    "Solution": ["approach", "version"],
    "DesignIntent": [],
    "Pitfall": ["symptom", "root_cause"],
}


def _norm(text: str | None) -> str:
    return (text or "").strip().lower()


async def find_exact_duplicates(session) -> list[dict]:
    """精确重复：同类型 + 归一化 (title, content) 相同的节点组。"""
    rows = (
        await session.execute(
            select(KnowledgeNode.id, KnowledgeNode.type, KnowledgeNode.title, KnowledgeNode.content)
            .where(
                KnowledgeNode.status == "approved",
                KnowledgeNode.is_deleted == False,  # noqa: E712
            )
        )
    ).all()
    groups: dict[tuple[str, str, str], list] = defaultdict(list)
    for nid, ntype, title, content in rows:
        groups[(ntype, _norm(title), _norm(content))].append((nid, title))
    return [
        {"node_type": k[0], "title": k[1], "count": len(v), "nodes": v}
        for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))
        if len(v) > 1
    ]


async def find_semantic_duplicates(session, *, threshold: float, node_type: str | None, limit: int) -> list[dict]:
    """语义重复：同类型 + 关键标识字段相同（L2 口径）+ facet 向量余弦 ≥ 阈值。

    向量口径与 calibrate_conflict_threshold 的 doc-doc 模式一致（落库 facet
    之间的余弦）；批量取节点向量后按关键标识分组，组内两两比对。
    """

    stmt = select(KnowledgeNode).where(
        KnowledgeNode.status == "approved",
        KnowledgeNode.is_deleted == False,  # noqa: E712
    )
    if node_type:
        stmt = stmt.where(KnowledgeNode.type == node_type)
    nodes = list((await session.execute(stmt)).scalars().all())
    if len(nodes) > 2000:
        print(f"[警告] approved 节点 {len(nodes)} 个，两两比对可能较慢；建议 --node-type 收窄")

    # 关键标识分组（L2 口径：全部关键标识相同才可比对）
    key_groups: dict[tuple, list[KnowledgeNode]] = defaultdict(list)
    for n in nodes:
        fields = KEY_IDENTITY_FIELDS.get(n.type, [])
        key = tuple(_norm(str((n.properties or {}).get(f) or "")) for f in fields)
        key_groups[(n.type, *key)].append(n)

    pairs: list[dict] = []
    for (_, _), group in key_groups.items():
        if len(group) < 2:
            continue
        vecs = {}
        for n in group:
            emb = (
                await session.execute(
                    select(NodeEmbedding.content_vector).where(
                        NodeEmbedding.node_id == n.id,
                        NodeEmbedding.facet == "content",
                    )
                )
            ).scalar_one_or_none()
            if emb is not None:
                vecs[n.id] = emb
        ids = list(vecs)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = vecs[ids[i]], vecs[ids[j]]
                dot = sum(x * y for x, y in zip(a, b))
                na = sum(x * x for x in a) ** 0.5
                nb = sum(x * x for x in b) ** 0.5
                cos = dot / (na * nb) if na and nb else 0.0
                if cos >= threshold:
                    pairs.append({
                        "similarity": round(cos, 4),
                        "node_a": str(ids[i]),
                        "node_b": str(ids[j]),
                    })
    pairs.sort(key=lambda p: -p["similarity"])
    return pairs[:limit]


async def main() -> None:
    parser = argparse.ArgumentParser(description="存量重复节点检测（只读）")
    parser.add_argument("--semantic", action="store_true", help="附加语义重复检测（向量比对，较慢）")
    parser.add_argument("--threshold", type=float, default=0.95, help="语义重复阈值（默认 0.95）")
    parser.add_argument("--node-type", default=None, help="限定节点类型（加速语义检测）")
    parser.add_argument("--limit", type=int, default=20, help="语义重复输出对数上限")
    args = parser.parse_args()

    async with AsyncSessionLocal() as session:
        exact = await find_exact_duplicates(session)
        print(f"== 精确重复（同类型+归一化 title/content 相同）：{len(exact)} 组")
        for g in exact[: args.limit]:
            print(f"  [{g['node_type']}] ×{g['count']}  {g['title'][:40]!r}")
            for nid, _ in g["nodes"][:5]:
                print(f"      {nid}")

        if args.semantic:
            pairs = await find_semantic_duplicates(
                session, threshold=args.threshold, node_type=args.node_type, limit=args.limit
            )
            print(f"\n== 语义重复（L2 关键标识相同 + 余弦 ≥ {args.threshold}）：{len(pairs)} 对")
            for p in pairs:
                print(f"  {p['similarity']}  {p['node_a'][:8]}.. ↔ {p['node_b'][:8]}..")

        if not exact:
            print("无精确重复")


if __name__ == "__main__":
    asyncio.run(main())
