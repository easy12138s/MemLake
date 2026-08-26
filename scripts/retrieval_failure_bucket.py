"""检索失败分桶评估脚本（只读，开发期使用）。

回答"是否值得引入图增强检索"：对项目内一组真实查询跑当前基线（向量+全文 RRF，
即 search_similar_requirements 同款链路），把未命中样例按 词法 / 图型 / 其他
分桶，并对比"图增强（以向量 top-1 为种子做图遍历扩展）"后的 hit@N / MRR，
量化图遍历对检索的收益。

分桶判据（引用 GraphRAG 决策指南）：
- 词法型（lexical）：golden 已被某文本引擎 top-k 召回，但融合/排序未进 top-N
  → 调融合/阈值/索引，与图无关
- 图型（graph）：golden 在文本引擎 top-k 之外，但沿图边从种子节点可达
  → 图增强检索的收益对象
- 其他（other）：数据缺失或查询过难（需人工复核）

运行（conda memlake 环境，本地 PG + embedding 容器已启动）：
    conda activate memlake
    $env:DATABASE_URL="postgresql+psycopg_async://memlake:memlake@localhost:5432/memlake"
    $env:EMBEDDING_HOST="localhost"
    python scripts/retrieval_failure_bucket.py --project-id <pid> --top-n 10
"""

import argparse
import asyncio
import os
import sys
import uuid

from sqlalchemy import select

from mem_lake.db.session import AsyncSessionLocal
from mem_lake.embedding.client import EmbeddingClient
from mem_lake.knowledge.age_store import get_graph_store
from mem_lake.knowledge.models import KnowledgeNode
from mem_lake.knowledge.repository import batch_regenerate_vectors
from mem_lake.search.filters import FilterSpec
from mem_lake.search.fusion import hybrid_search

# 每个用例：query（真实研发提问）+ golden（期望命中的节点标题子串）+ category
CASES: list[dict] = [
    {"query": "审批批次状态机如何流转和幂等", "golden": ["approval.service：审批批次状态机"], "cat": "single"},
    {"query": "网关的认证鉴权限流审计中间件", "golden": ["gateway.middleware：认证/RBAC/限流/审计中间件"], "cat": "single"},
    {"query": "向量检索用什么算子", "golden": ["search.vector：VectorSearcher", "踩坑：pgvector 内积 API"], "cat": "single"},
    {"query": "三引擎并行检索与 RRF 融合的实现方案和设计决策", "golden": ["search.fusion：三引擎并行", "方案：三引擎并行检索 + RRF", "设计决策：三引擎并行 + RRF 融合"], "cat": "multi"},
    {"query": "数据库备份恢复的方案与设计决策", "golden": ["deploy/backup：pg_dump", "方案：pg_dump 自定义格式备份与恢复", "设计决策：pg_dump 全量备份恢复"], "cat": "multi"},
    {"query": "AGE 图存储的踩坑与实现", "golden": ["knowledge.age_store：AGEGraphStore", "踩坑：AGE 边创建端点缺失时静默丢边"], "cat": "multi"},
    {"query": "rerank 精排的降级处理", "golden": ["踩坑：rerank 缺失时检索静默降级", "设计决策：RRF 后接入可降级 rerank 精排"], "cat": "multi"},
    {"query": "AsyncSession 并发安全与独立会话", "golden": ["踩坑：AsyncSession 非并发安全", "设计决策：三引擎各独立 AsyncSession 并行"], "cat": "sparse"},
    {"query": "HNSW 索引 opclass 迁移", "golden": ["踩坑：HNSW 需 DROP 重建迁移 opclass"], "cat": "single"},
    {"query": "冲突检测的三层与 L0 硬键判定", "golden": ["approval.conflict：三层冲突检测", "设计决策：冲突检测 L0 硬键判定"], "cat": "multi"},
    {"query": "per AccessKey 令牌桶限流", "golden": ["设计决策：per AccessKey 令牌桶限流"], "cat": "single"},
    {"query": "pg_dump 备份命令", "golden": ["deploy/backup：pg_dump"], "cat": "exact"},
    {"query": "embedding 镜像 CPU 与 torch 下载", "golden": ["deploy/Dockerfile.embedding", "踩坑：CPU torch 需指定国内镜像通道"], "cat": "multi"},
    {"query": "审计日志 append-only 实现", "golden": ["audit.service：append-only 审计"], "cat": "single"},
    {"query": "中文全文检索 tsvector", "golden": ["search.fulltext：tsvector 中文全文检索"], "cat": "single"},
    {"query": "Embedding 服务与 rerank 端点", "golden": ["deploy/embedding_server", "embedding.client：Embedding/Rerank"], "cat": "single"},
    {"query": "网关启动初始化与建表迁移", "golden": ["gateway.server：MCP 网关入口与启动初始化", "db.init：扩展校验、幂等建表"], "cat": "multi"},
    {"query": "异步向量重嵌后台 worker", "golden": ["gateway.background_tasks：异步向量重嵌 worker"], "cat": "single"},
]


async def load_nodes(session, project_id: uuid.UUID) -> list[KnowledgeNode]:
    stmt = select(KnowledgeNode).where(
        KnowledgeNode.project_id == project_id,
        KnowledgeNode.status == "approved",
        KnowledgeNode.is_deleted.is_(False),
    )
    return list((await session.execute(stmt)).scalars())


def resolve_golden(nodes: list[KnowledgeNode], subtitles: list[str]) -> set[uuid.UUID]:
    found: set[uuid.UUID] = set()
    for n in nodes:
        if any(s in n.title for s in subtitles):
            found.add(n.id)
    return found


def mrri(hits_ordered: list[int], n: int) -> float:
    """平均倒数排名（仅统计 top-n 内首个命中）。无命中记 0。"""
    if not hits_ordered:
        return 0.0
    first = hits_ordered[0]
    return 1.0 / first if first <= n else 0.0


async def reindex_project(project_id: uuid.UUID, embedding_client: EmbeddingClient) -> int:
    """对项目存量 approved 节点回填多向量 facets。

    注意：batch_regenerate_vectors 会把整批节点的全部 facet 文本一次 embed，
    而 embedding 服务单次上限 MAX_EMBED_TEXTS=128（且节点多时一次 embed 易超时），
    故这里按 20 节点/批小步推进，避免 422 与 ReadTimeout。
    """
    async with AsyncSessionLocal() as session:
        nodes = await load_nodes(session, project_id)
    if not nodes:
        return 0
    chunk_size = 20
    for i in range(0, len(nodes), chunk_size):
        chunk = nodes[i : i + chunk_size]
        async with AsyncSessionLocal() as session:
            await batch_regenerate_vectors(
                session, embedding_client=embedding_client, nodes=chunk, actor="retrieval-eval"
            )
            await session.commit()
        print(f"  reindex 进度 {min(i + chunk_size, len(nodes))}/{len(nodes)}")
    return len(nodes)


async def evaluate(project_id: uuid.UUID, top_n: int, graph_depth: int) -> dict:
    embedding_client = EmbeddingClient(
        base_url=f"http://{os.environ.get('EMBEDDING_HOST', 'localhost')}:8001",
        dimension=1024,
    )
    graph_store = get_graph_store()

    async with AsyncSessionLocal() as session:
        nodes = await load_nodes(session, project_id)

    rows: list[dict] = []
    for case in CASES:
        golden = resolve_golden(nodes, case["golden"])
        if not golden:
            rows.append(
                {
                    "query": case["query"],
                    "cat": case["cat"],
                    "golden": case["golden"],
                    "golden_ids": [],
                    "note": "golden 未在库中找到（标题不匹配或数据缺失），跳过判定",
                }
            )
            continue

        filters = FilterSpec(project_id=project_id)
        base = await hybrid_search(
            query=case["query"],
            embedding_client=embedding_client,
            graph_store=graph_store,
            top_k=50,
            top_n=top_n,
            filters=filters,
        )
        fused_ids = [r.node_id for r in base["fused"]]
        vector_ids = {r.node_id for r in base["vector"]}
        fulltext_ids = {r.node_id for r in base["fulltext"]}

        hit_positions = [i + 1 for i, nid in enumerate(fused_ids) if nid in golden]
        hit = bool(hit_positions)

        # 图增强：以向量 top-1 为种子做图遍历，作为补充候选（不动 RRF 排序）
        seed = None
        if base["vector"]:
            seed = base["vector"][0].node_id
        graph_ids: set[uuid.UUID] = set()
        if seed is not None:
            enhanced = await hybrid_search(
                query=case["query"],
                embedding_client=embedding_client,
                graph_store=graph_store,
                top_k=50,
                top_n=top_n,
                filters=filters,
                graph_node_id=seed,
                graph_depth=graph_depth,
            )
            graph_ids = {r.node_id for r in enhanced["graph"]}

        enhanced_ordered = list(dict.fromkeys(fused_ids + [g for g in graph_ids if g not in set(fused_ids)]))
        enh_positions = [i + 1 for i, nid in enumerate(enhanced_ordered) if nid in golden]
        enh_hit = bool(enh_positions)

        # 分桶：对基线未命中的样例归类
        bucket = None
        if not hit:
            if golden & (vector_ids | fulltext_ids):
                bucket = "lexical"
            elif golden & graph_ids:
                bucket = "graph"
            else:
                bucket = "other"

        rows.append(
            {
                "query": case["query"],
                "cat": case["cat"],
                "golden": case["golden"],
                "golden_ids": list(golden),
                "hit": hit,
                "hit_pos": hit_positions[0] if hit_positions else None,
                "bucket": bucket,
                "vector_found": bool(golden & vector_ids),
                "fulltext_found": bool(golden & fulltext_ids),
                "graph_found": bool(golden & graph_ids),
                "enh_hit": enh_hit,
                "enh_pos": enh_positions[0] if enh_positions else None,
            }
        )

    return {"rows": rows}


def report(project_id: uuid.UUID, top_n: int, result: dict) -> None:
    rows = [r for r in result["rows"] if r.get("golden_ids")]
    print(f"\n=== 检索失败分桶评估  project={project_id}  top_n={top_n}  用例数={len(rows)} ===")
    print(f"{'#':<3}{'类别':<8}{'命中':<6}{'桶':<9}{'向量':<6}{'全文':<6}{'图':<5}{'增强':<6} 查询")
    for i, r in enumerate(rows, 1):
        print(
            f"{i:<3}{r['cat']:<8}{'Y' if r['hit'] else 'N':<6}"
            f"{(r['bucket'] or '-'):<9}{'Y' if r['vector_found'] else 'N':<6}"
            f"{'Y' if r['fulltext_found'] else 'N':<6}{'Y' if r['graph_found'] else 'N':<5}"
            f"{'Y' if r['enh_hit'] else 'N':<6}{r['query']}"
        )
        if not r["hit"] and r["bucket"] == "graph":
            print(f"     ↑ 图型失败：文本引擎未召回，但沿种子可达 golden={r['golden']}")

    # 统计
    hits = [r for r in rows if r["hit"]]
    enh_hits = [r for r in rows if r["enh_hit"]]
    fails = [r for r in rows if not r["hit"]]
    buckets: dict[str, int] = {}
    for r in fails:
        buckets[r["bucket"]] = buckets.get(r["bucket"], 0) + 1
    for b in ("lexical", "graph", "other"):
        buckets.setdefault(b, 0)

    n = len(rows)
    print(f"\n基线 hit@{top_n}={len(hits)}/{n}={len(hits) / n:.2%}")
    print(f"图增强 hit@{top_n}={len(enh_hits)}/{n}={len(enh_hits) / n:.2%}")
    print(f"失败 {len(fails)} 条：lexical={buckets['lexical']} graph={buckets['graph']} other={buckets['other']}")
    if fails:
        graph_ratio = buckets["graph"] / len(fails)
        print(f"图型失败占比 = {buckets['graph']}/{len(fails)} = {graph_ratio:.1%}")
        print(f"结论：{'≥30% → 建议投入图增强检索' if graph_ratio >= 0.3 else '<30% → 暂不投入，先优化文本检索'}")
    else:
        print("无失败样例。")


def main() -> None:
    # Windows 下 psycopg3 async 需 Selector 事件循环（与 tests/conftest.py 一致）
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    parser = argparse.ArgumentParser(description="检索失败分桶评估（只读）")
    parser.add_argument("--project-id", required=True, help="评估的项目 ID")
    parser.add_argument("--top-n", type=int, default=10, help="判定命中的 top-N（默认 10）")
    parser.add_argument("--graph-depth", type=int, default=2, help="图增强遍历深度（默认 2）")
    parser.add_argument(
        "--reindex",
        action="store_true",
        help="评估前先对项目存量节点回填多向量 facets（多向量改造后首次评估需开启）",
    )
    args = parser.parse_args()

    project_id = uuid.UUID(args.project_id)

    if args.reindex:
        # 批量回填时长文本 embed 可能超过默认 30s，用长超时客户端
        embedding_client = EmbeddingClient(
            base_url=f"http://{os.environ.get('EMBEDDING_HOST', 'localhost')}:8001",
            dimension=1024,
            timeout=180.0,
        )
        count = asyncio.run(reindex_project(project_id, embedding_client))
        print(f"reindex 完成：{count} 个节点已回填 facets")

    result = asyncio.run(evaluate(project_id, args.top_n, args.graph_depth))
    report(project_id, args.top_n, result)


if __name__ == "__main__":
    main()
