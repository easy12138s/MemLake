"""冲突检测阈值标定脚本（只读，勿在生产写入路径上运行）。

背景：CONFLICT_SIMILARITY_THRESHOLD（config.py 默认 0.85）换 embedding 模型后需按
本项目真实数据重新标定（Qwen3-Embedding-0.6B 的余弦分布与旧模型不同）。

两种标定模式：
1. doc-doc 模式（默认）：直接比较落库向量（content_vector）之间的余弦。
   无外部依赖，但与运行时分布有偏差。
2. query-doc 模式（--embedding-url）：将 f"{title}\\n{content}" 以 prompt_name="query"
   指令感知编码后与落库向量比对——与 detect_conflicts 运行时条件完全一致，推荐使用。
   实测 Qwen3 下两种模式分布差异显著（同对分数可差 >0.1），阈值标定应以 query-doc 为准。

与检测器对齐的三个口径（与 approval/conflict.py 保持一致）：
- 只统计同类型节点对（运行时 L1 按 node_type 过滤，跨类型对不会成为候选）
- "疑似重复"要求全部关键标识字段相同（与 L2 _match_key_attrs / L0 一致，
  任一字段相同不算——例如仅 file_path 相同、name 不同的两个 CodeSnippet 不是重复）
- 相似度基于 f"{title}\\n{content}" 的向量（与存储/查询侧构造一致）

做法：连库读取 approved 节点，按上述口径把同类型节点对分成 疑似重复 / 一般不重复
两组，分别计算相似度分布，输出分位点与 Top 对明细，并给出建议阈值区间
（取 一般不重复组 p95 → 疑似重复组 p5）。生产库中同实体重复写入会被 L0 拦截，
疑似重复组样本通常很少，此时脚本会提示样本不足，应人工检视 Top 对明细定阈值。

用法（容器内执行）：
    docker cp scripts/calibrate_conflict_threshold.py deploy-mem-lake-1:/tmp/calibrate.py
    docker exec deploy-mem-lake-1 python /tmp/calibrate.py \
        --database-url postgresql://memlake:memlake@postgres:5432/memlake \
        --project-id <pid> --embedding-url http://embedding:8001/embed

参数：
    --database-url  DATABASE_URL（psycopg 格式；容器内异步 URL 需去掉 +psycopg_async）
    --project-id    限定项目（建议按项目标定，跨项目向量空间可能不同）
    --embedding-url embedding 服务 /embed 端点；传入即启用 query-doc 运行时模式
    --limit         最多加载的节点数（默认 2000）
    --pairs         最多采样的对数（默认 200000，控制计算量）
    --max-title-len 规范化标题时的取前 N 字符（默认 40）

标定记录：
    2026-08-22 Qwen3-Embedding-0.6B（query-doc 模式）：
    MemLake 项目相关不同实体最高 0.772、近重复 0.920/0.927；ReqRadar 相关不同实体
    最高 0.764、同实体对 0.837/0.914/0.975（其中 0.837 一对关键标识不同，按 L2 设计
    排除、与阈值无关）。0.85 落在跨项目空隙内且余量均衡（+0.08/-0.07），维持不变。
"""

import argparse
import json
import math
import os
import random
import urllib.request
from typing import Any

# 与 approval/conflict.py 保持一致的关键标识字段（用于"疑似重复对"判定）
KEY_IDENTITY_FIELDS: dict[str, list[str]] = {
    "Requirement": ["requirement_id"],
    "CodeSnippet": ["name", "file_path"],
    "Solution": ["approach"],
    "DesignIntent": ["rationale"],
    "Pitfall": ["symptom"],
    "Decision": ["decision_id"],
    "ProjectProfile": ["name"],
}


def cosine(a: list[float], b: list[float]) -> float:
    """两向量的余弦相似度（假定已归一化；若未归一化则按定义计算）。"""
    if len(a) != len(b):
        raise ValueError("向量维度不一致")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def parse_vector(text: str | None) -> list[float]:
    """解析 pgvector::text 的向量文本（"[0.1,0.2,...]"）为浮点列表。"""
    if not text:
        return []
    try:
        val = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(val, list) and all(isinstance(x, (int, float)) for x in val):
        return [float(x) for x in val]
    return []


def _norm_title(title: str | None, max_len: int = 40) -> str:
    """规范化标题：去空白 + 统一大小写 + 截断，用于简化"疑似重复"判定。"""
    if not title:
        return ""
    t = " ".join(title.split()).lower()
    return t[:max_len]


def _is_duplicate_pair(
    a_type: str,
    a_props: dict,
    a_title: str,
    b_type: str,
    b_props: dict,
    b_title: str,
    max_title_len: int,
) -> bool:
    """判定两节点是否"疑似重复"。

    与 conflict._match_key_attrs / L0 同口径：全部关键标识字段均相同才视为同一实体
    （任一字段相同不算——仅 file_path 相同、name 不同的两个 CodeSnippet 不是重复）。
    规范化标题相同（非空）也视为疑似重复（内容大概率高度相近）。
    """
    if a_type != b_type:
        return False
    if _norm_title(a_title, max_title_len) and _norm_title(
        a_title, max_title_len
    ) == _norm_title(b_title, max_title_len):
        return True
    key_fields = KEY_IDENTITY_FIELDS.get(a_type, [])
    if key_fields and all(
        a_props.get(f) is not None and a_props.get(f) == b_props.get(f)
        for f in key_fields
    ):
        return True
    return False


def embed_queries(
    embedding_url: str, texts: list[str], batch_size: int = 32, timeout: float = 120.0
) -> list[list[float]]:
    """以 prompt_name="query" 指令感知批量编码查询文本（与 VectorSearcher 检索侧一致）。"""
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        chunk = texts[start : start + batch_size]
        body = json.dumps({"texts": chunk, "prompt_name": "query"}).encode()
        req = urllib.request.Request(
            embedding_url, data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(chunk):
            raise RuntimeError(f"embedding 服务响应格式错误: {data}")
        vectors.extend(embeddings)
    return vectors


def analyze_distribution(
    nodes: list[dict[str, Any]],
    *,
    pairs: int = 200_000,
    max_title_len: int = 40,
    seed: int = 42,
    query_vectors: list[list[float] | None] | None = None,
) -> dict[str, Any]:
    """纯函数：采样同类型节点对，统计相似度分布并给出建议阈值。

    参数：
        nodes: [{"id","type","title","properties","vector"}, ...]，vector 为已解析浮点列表
        pairs: 最多采样的对数
        query_vectors: 与 nodes 等长的 query 侧指令感知向量（None 元素跳过）。
            传入时为 query-doc 模式（与 detect_conflicts 运行时一致，相似度取
            双向最大值）；None 时为 doc-doc 模式（直接比较落库向量）。
        返回分布统计（供单测与无 DB 场景复用）。
    """
    if query_vectors is not None and len(query_vectors) != len(nodes):
        raise ValueError("query_vectors 与 nodes 长度不一致")

    use_query = query_vectors is not None
    usable = [
        i
        for i, n in enumerate(nodes)
        if len(n.get("vector", [])) > 0
        and (not use_query or (query_vectors[i] is not None and len(query_vectors[i]) > 0))
    ]
    dup_scores: list[float] = []
    nondup_scores: list[float] = []
    top_pairs: list[dict[str, Any]] = []
    rng = random.Random(seed)

    total = len(usable)
    if total < 2:
        return {
            "mode": "query-doc" if use_query else "doc-doc",
            "total_nodes": total,
            "sampled_pairs": 0,
            "dup_samples": 0,
            "nondup_samples": 0,
            "dup_distribution": {},
            "nondup_distribution": {},
            "suggested_threshold": None,
            "top_pairs": [],
            "note": "有效节点不足 2 个，无法标定",
        }

    def _pair_score(i: int, j: int) -> float:
        a, b = nodes[i], nodes[j]
        if use_query:
            # 双向最大：贴近"任一方作为新节点提交时召回另一方"的运行时视角
            return max(
                cosine(query_vectors[i], b["vector"]),
                cosine(query_vectors[j], a["vector"]),
            )
        return cosine(a["vector"], b["vector"])

    # 候选对池：同类型（运行时 L1 按 node_type 过滤，跨类型对不会成为候选）
    same_type: list[tuple[int, int]] = []
    for x in range(total):
        for y in range(x + 1, total):
            i, j = usable[x], usable[y]
            if nodes[i]["type"] == nodes[j]["type"]:
                same_type.append((i, j))

    actual = min(pairs, len(same_type), 500_000)
    sampled = same_type if actual == len(same_type) else rng.sample(same_type, actual)
    for i, j in sampled:
        a, b = nodes[i], nodes[j]
        score = _pair_score(i, j)
        dup = _is_duplicate_pair(
            a["type"], a["properties"], a["title"],
            b["type"], b["properties"], b["title"],
            max_title_len,
        )
        (dup_scores if dup else nondup_scores).append(score)
        top_pairs.append(
            {
                "score": round(score, 4),
                "type": a["type"],
                "title_a": a["title"],
                "title_b": b["title"],
                "dup": dup,
            }
        )

    top_pairs.sort(key=lambda p: -p["score"])
    top_pairs = top_pairs[:10]

    def _percentiles(values: list[float]) -> dict[str, float]:
        if not values:
            return {}
        s = sorted(values)
        n = len(s)
        return {
            "p5": s[min(int(n * 0.05), n - 1)],
            "p50": s[min(int(n * 0.50), n - 1)],
            "p95": s[min(int(n * 0.95), n - 1)],
            "mean": sum(s) / n,
        }

    dup_dist = _percentiles(dup_scores)
    nondup_dist = _percentiles(nondup_scores)

    # 建议间隔：重复组相似度高、一般不重复组相似度低。
    # 合理阈值应落在 非重复组上尾(p95) 与 重复组下尾(p5) 之间；
    # 当二者无重叠（非重复p95 <= 重复p5）时给出区间与推荐中点，有重叠则无法可靠区分。
    suggested = None
    if dup_scores and nondup_scores:
        low = nondup_dist["p95"]
        high = dup_dist["p5"]
        if low <= high:
            suggested = {
                "low": round(low, 3),
                "high": round(high, 3),
                "recommended": round((low + high) / 2, 3),
            }

    note = None
    if len(dup_scores) < 5:
        note = (
            "疑似重复样本不足（生产库中同实体重复写入会被 L0 拦截，属正常现象）。"
            "请人工检视 Top 对明细：阈值应高于'相关但不同实体'对的最高分，"
            "低于'同实体改写'对的最低分。"
        )

    return {
        "mode": "query-doc" if use_query else "doc-doc",
        "total_nodes": total,
        "sampled_pairs": actual,
        "dup_samples": len(dup_scores),
        "nondup_samples": len(nondup_scores),
        "dup_distribution": dup_dist,
        "nondup_distribution": nondup_dist,
        "suggested_threshold": suggested,
        "top_pairs": top_pairs,
        "note": note,
    }


def load_nodes(database_url: str, project_id: str | None, limit: int) -> list[dict[str, Any]]:
    """从库加载 approved 节点（id/type/title/content/properties/向量文本）。"""
    import psycopg

    where = "status = 'approved' AND is_deleted = false"
    params: list[Any] = []
    if project_id:
        where += " AND project_id = %s"
        params.append(project_id)

    sql = (
        "SELECT id, type, title, content, properties::text, content_vector::text "
        "FROM knowledge_node WHERE " + where + " LIMIT %s"
    )
    params.append(limit)

    out: list[dict[str, Any]] = []
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for row in cur:
                props = {}
                if row[4]:
                    try:
                        props = json.loads(row[4])
                    except json.JSONDecodeError:
                        props = {}
                out.append(
                    {
                        "id": str(row[0]),
                        "type": row[1],
                        "title": row[2],
                        "content": row[3] or "",
                        "properties": props,
                        "vector": parse_vector(row[5]),
                    }
                )
    return out


def _fmt_dist(name: str, dist: dict[str, float]) -> str:
    if not dist:
        return f"{name}: 无样本"
    return (
        f"{name}: p5={dist['p5']:.3f} p50={dist['p50']:.3f} "
        f"p95={dist['p95']:.3f} mean={dist['mean']:.3f}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="冲突检测阈值标定脚本（只读）")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--project-id", default=None)
    parser.add_argument(
        "--embedding-url",
        default=None,
        help="embedding 服务 /embed 端点；传入即启用 query-doc 运行时模式（推荐）",
    )
    parser.add_argument("--limit", type=int, default=2000)
    parser.add_argument("--pairs", type=int, default=200_000)
    parser.add_argument("--max-title-len", type=int, default=40)
    args = parser.parse_args()

    if not args.database_url:
        parser.error("缺少 --database-url 或环境变量 DATABASE_URL")
    if not args.project_id:
        print("警告: 未指定 --project-id，将跨全部项目采样（不同项目向量空间可能不同，建议按项目标定）")

    nodes = load_nodes(args.database_url, args.project_id, args.limit)

    query_vectors = None
    if args.embedding_url:
        # 与 detect_conflicts 一致：查询文本 = f"{title}\n{content}"，指令感知 query 侧编码
        texts = [f"{n['title']}\n{n['content']}" for n in nodes]
        print(f"query 侧向量化中（{len(texts)} 条，prompt_name=query）...")
        qvecs = embed_queries(args.embedding_url, texts)
        # 与 nodes 对齐：无落库向量的节点 query 向量也置 None（不参与采样）
        query_vectors = [
            q if len(n["vector"]) > 0 else None for q, n in zip(qvecs, nodes)
        ]

    result = analyze_distribution(
        nodes,
        pairs=args.pairs,
        max_title_len=args.max_title_len,
        query_vectors=query_vectors,
    )

    print(f"\n标定模式: {result['mode']}")
    print(f"有效节点数: {result['total_nodes']}")
    print(f"采样对数: {result['sampled_pairs']} "
          f"(疑似重复 {result['dup_samples']} / 一般 {result['nondup_samples']})")
    print(_fmt_dist("疑似重复", result["dup_distribution"]))
    print(_fmt_dist("一般不重复", result["nondup_distribution"]))

    print("\n=== Top 10 相似对（人工检视用）===")
    for p in result["top_pairs"]:
        mark = "DUP?" if p["dup"] else "    "
        print(f"{p['score']:.3f} {mark} [{p['type']}] {p['title_a'][:38]!r} <-> {p['title_b'][:38]!r}")

    if result["suggested_threshold"]:
        s = result["suggested_threshold"]
        print(f"\n建议阈值区间: [{s['low']}, {s['high']}]")
        print(f"建议将 CONFLICT_SIMILARITY_THRESHOLD 设为区间内的一个值（如 {s['low']:.3f}）")
    else:
        print(f"\n无法自动给出建议阈值：{result.get('note', '样本不足或分布重叠')}")
    if result["note"] and result["suggested_threshold"]:
        print(f"注意: {result['note']}")


if __name__ == "__main__":
    main()
