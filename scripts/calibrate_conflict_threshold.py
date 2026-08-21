"""冲突检测阈值标定脚本（只读，勿在生产写入路径上运行）。

背景：CONFLICT_SIMILARITY_THRESHOLD（config.py 默认 0.85）换 embedding 模型后需按
本项目真实数据重新标定（Qwen3-Embedding-0.6B 的余弦分布与旧模型不同）。

做法：连库读取 approved 节点，按"是否疑似重复"（节点类型的关键标识字段相同，或
规范化标题相同）把节点对分成 疑似重复 / 一般不重复 两组，分别计算 content_vector 的
余弦分布，输出分位点并给出建议阈值区间（取 疑似重复组 5 分位 → 一般不重复组 95 分位 之间）。

用法（容器内执行）：
    docker cp scripts/calibrate_conflict_threshold.py deploy-mem-lake-1:/tmp/calibrate.py
    docker exec deploy-mem-lake-1 python /tmp/calibrate.py --database-url "$DATABASE_URL" \
        --project-id <pid> --limit 2000 --pairs 200000

参数：
    --database-url  DATABASE_URL（默认读取环境变量）
    --project-id    限定项目（建议按项目标定，跨项目向量空间可能不同）
    --limit         最多加载的节点数（默认 2000）
    --pairs         最多采样的对数（默认 200000，控制计算量）
    --max-title-len 规范化标题时的取前 N 字符（默认 40）

输出建议阈值可作为 CONFLICT_SIMILARITY_THRESHOLD 的参考起点。
"""

import argparse
import json
import math
import os
import random
from typing import Any

# 与 approval/conflict.py 保持一致的关键标识字段（用于"疑似重复对"判定）
KEY_IDENTITY_FIELDS: dict[str, list[str]] = {
    "Requirement": ["requirement_id"],
    "CodeSnippet": ["name", "file_path"],
    "Solution": ["approach"],
    "DesignIntent": ["rationale"],
    "Decision": ["decision_id"],
    "Pitfall": ["symptom"],
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
    """判定两节点是否"疑似重复"（关键标识字段均相同，或规范化标题相同）。"""
    if a_type != b_type:
        return False
    if _norm_title(a_title, max_title_len) and _norm_title(
        a_title, max_title_len
    ) == _norm_title(b_title, max_title_len):
        return True
    for field in KEY_IDENTITY_FIELDS.get(a_type, []):
        if a_props.get(field) is not None and a_props.get(field) == b_props.get(field):
            return True
    return False


def analyze_distribution(
    nodes: list[dict[str, Any]],
    *,
    pairs: int = 200_000,
    max_title_len: int = 40,
    seed: int = 42,
) -> dict[str, Any]:
    """纯函数：采样节点对，统计余弦分布并给出建议阈值。

    参数：
        nodes: [{"id","type","title","properties","vector"}, ...]，vector 为已解析浮点列表
        pairs: 最多采样的对数
        返回分布统计（供单测与无 DB 场景复用）。
    """
    vectorized = [n for n in nodes if len(n.get("vector", [])) > 0]
    dup_scores: list[float] = []
    nondup_scores: list[float] = []
    rng = random.Random(seed)

    total = len(vectorized)
    if total < 2:
        return {
            "total_nodes": total,
            "sampled_pairs": 0,
            "dup_samples": 0,
            "nondup_samples": 0,
            "dup_distribution": {},
            "nondup_distribution": {},
            "suggested_threshold": None,
            "note": "有效节点不足 2 个，无法标定",
        }

    max_pairs = total * (total - 1) // 2
    actual = min(pairs, max_pairs, 500_000)
    for _ in range(actual):
        i, j = rng.sample(range(total), 2)
        a, b = vectorized[i], vectorized[j]
        score = cosine(a["vector"], b["vector"])
        if _is_duplicate_pair(
            a["type"], a["properties"], a["title"],
            b["type"], b["properties"], b["title"],
            max_title_len,
        ):
            dup_scores.append(score)
        else:
            nondup_scores.append(score)

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

    return {
        "total_nodes": total,
        "sampled_pairs": actual,
        "dup_samples": len(dup_scores),
        "nondup_samples": len(nondup_scores),
        "dup_distribution": dup_dist,
        "nondup_distribution": nondup_dist,
        "suggested_threshold": suggested,
    }


def load_nodes(database_url: str, project_id: str | None, limit: int) -> list[dict[str, Any]]:
    """从库加载 approved 节点（id/type/title/properties/向量文本）。"""
    import psycopg

    where = "status = 'approved' AND is_deleted = false"
    params: list[Any] = []
    if project_id:
        where += " AND project_id = %s"
        params.append(project_id)

    sql = (
        "SELECT id, type, title, properties::text, content_vector::text "
        "FROM knowledge_node WHERE " + where + " LIMIT %s"
    )
    params.append(limit)

    out: list[dict[str, Any]] = []
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for row in cur:
                props = {}
                if row[3]:
                    try:
                        props = json.loads(row[3])
                    except json.JSONDecodeError:
                        props = {}
                out.append(
                    {
                        "id": str(row[0]),
                        "type": row[1],
                        "title": row[2],
                        "properties": props,
                        "vector": parse_vector(row[4]),
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
    parser.add_argument("--limit", type=int, default=2000)
    parser.add_argument("--pairs", type=int, default=200_000)
    parser.add_argument("--max-title-len", type=int, default=40)
    args = parser.parse_args()

    if not args.database_url:
        parser.error("缺少 --database-url 或环境变量 DATABASE_URL")
    if not args.project_id:
        print("警告: 未指定 --project-id，将跨全部项目采样（不同项目向量空间可能不同，建议按项目标定）")

    nodes = load_nodes(args.database_url, args.project_id, args.limit)
    result = analyze_distribution(
        nodes, pairs=args.pairs, max_title_len=args.max_title_len
    )

    print(f"有效节点数: {result['total_nodes']}")
    print(f"采样对数: {result['sampled_pairs']} "
          f"(疑似重复 {result['dup_samples']} / 一般 {result['nondup_samples']})")
    print(_fmt_dist("疑似重复", result["dup_distribution"]))
    print(_fmt_dist("一般不重复", result["nondup_distribution"]))
    if result["suggested_threshold"]:
        s = result["suggested_threshold"]
        print(f"建议阈值区间: [{s['low']}, {s['high']}]")
        print(f"建议将 CONFLICT_SIMILARITY_THRESHOLD 设为区间内的一个值（如 {s['low']:.3f}）")
    else:
        print("无法给出建议阈值：" + result.get("note", "样本不足或分布重叠"))


if __name__ == "__main__":
    main()
