"""图节点 system_id 回填脚本（幂等可重跑）。

背景：FIX-05 之前 add_node 的 Cypher 只写 id/project_id/title 三字段，
graph_props（repository.py _graph_props）构造的 system_id 被静默丢弃。本脚本
从 knowledge_node 表读取 id → system_id 映射，通过 Cypher 批量 SET 到图节点，
补齐图侧 system 维度（ENH-01 图能力规划前置）。

幂等：MATCH 后 SET 属性为固定值，重复执行结果不变，可安全重跑。

用法（容器内执行，需 AGE 图存在）：
    docker cp scripts/backfill_graph_system_id.py deploy-mem-lake-1:/tmp/backfill.py
    docker exec deploy-mem-lake-1 python /tmp/backfill.py \
        --database-url postgresql://memlake:memlake@postgres:5432/memlake \
        --graph-name mem_lake_graph

参数：
    --database-url  DATABASE_URL（psycopg 格式；容器内异步 URL 需去掉 +psycopg_async）
    --graph-name    AGE 图名（默认 mem_lake_graph，与 .env AGE_GRAPH_NAME 对齐）
    --limit         最多回填节点数（默认 0=全部；测试可传小值抽查）
    --dry-run       只统计不执行 SET，输出将回填的节点数
"""

import argparse
import json
import sys
import uuid

import psycopg


def _execute_cypher(conn, graph_name: str, cypher: str, params: dict) -> list:
    """执行单条 Cypher（经 PREPARE/EXECUTE 参数化，graph_name 为字面量）。"""
    stmt_name = f"backfill_{uuid.uuid4().hex[:8]}"
    # 值统一转字符串（psycopg 返回的 UUID 等对象不可 JSON 序列化；Cypher 参数为标量）
    params_json = json.dumps({k: str(v) for k, v in params.items()})
    with conn.cursor() as cur:
        cur.execute(
            f"PREPARE {stmt_name}(agtype) AS "
            f"SELECT * FROM cypher('{graph_name}', $age$ {cypher} $age$, $1) "
            f"AS (result agtype)"
        )
        tag = f"p{uuid.uuid4().hex[:8]}"
        cur.execute(f"EXECUTE {stmt_name}(${tag}${params_json}${tag}$)")
        rows = cur.fetchall()
        cur.execute(f"DEALLOCATE {stmt_name}")
        return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="图节点 system_id 回填")
    parser.add_argument("--database-url", required=True, help="psycopg 格式 DATABASE_URL")
    parser.add_argument("--graph-name", default="mem_lake_graph", help="AGE 图名")
    parser.add_argument("--limit", type=int, default=0, help="最多回填节点数（0=全部）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不执行")
    args = parser.parse_args()

    conn = psycopg.connect(args.database_url)
    try:
        # 会话级 AGE 加载与 search_path（与 age_store._ensure_age_session 同口径）
        with conn.cursor() as cur:
            cur.execute("LOAD 'age'")
            cur.execute("SET search_path = ag_catalog, public")

        with conn.cursor() as cur:
            # 收集 id → system_id 映射（system_id 为空则回填空串，与图节点写入口径一致）
            sql = (
                "SELECT id, COALESCE(system_id::text, '') FROM knowledge_node "
                "WHERE is_deleted = false"
            )
            if args.limit > 0:
                sql += f" LIMIT {args.limit}"
            cur.execute(sql)
            rows = cur.fetchall()

        if not rows:
            print("无待回填节点（knowledge_node 表为空）")
            return 0

        if args.dry_run:
            print(f"[dry-run] 将回填 {len(rows)} 个图节点的 system_id")
            return 0

        updated = 0
        for node_id, system_id in rows:
            _execute_cypher(
                conn,
                args.graph_name,
                "MATCH (n {id: $node_id}) SET n.system_id = $system_id",
                {"node_id": node_id, "system_id": system_id},
            )
            updated += 1
        conn.commit()
        print(f"已回填 {updated} 个图节点的 system_id（幂等，可重跑）")

        # 抽查：统计图节点 system_id 非空数量，验证回填落库
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM cypher('{args.graph_name}', "
                "$age$ MATCH (n) WHERE n.system_id IS NOT NULL AND n.system_id <> '' "
                "RETURN count(n) AS cnt $age$) AS (result agtype)"
            )
            sample = cur.fetchall()
        print(f"图节点 system_id 非空数: {sample[0][0] if sample else 'unknown'}")
        return 0
    except psycopg.Error as exc:
        conn.rollback()
        print(f"回填失败: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
