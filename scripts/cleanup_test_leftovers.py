"""测试残留数据清理脚本（一次性运维操作，非常驻代码）。

背景：共享 dev DB 长期被 e2e/集成测试污染（随机 system/project、mock
向量、悬空引用）。本脚本按「保留白名单」口径清点/删除白名单外的一切：
删除范围 = 既不归属保留 system 域、也不归属保留项目的全部知识数据及其
关联行（向量 / AGE 图顶点 / system 域行 / 审批批次 / 重索引任务 / 孤儿向量）。

固定口径（不提供开关，调整时改本常量并评审）：
- audit_log：一律保留（append-only 契约；操作历史有溯源价值）
- access_key：一律不动（无凭据判断依据，宁留勿删）
- embedding_state / EmbeddingState：模型签名单例，不动

保留白名单（CLI 指定，至少一个 system）：
- --keep-system <名称或UUID>：可多次；name 优先（存量 system 常无 code）
- --keep-project <UUID>：可多次（项目域保留；项目无名称字段，仅支持 UUID）

用法（宿主机 venv 或任意能连 DB+AGE 的环境）：
    # dry-run：只清点（默认）
    python -m scripts.cleanup_test_leftovers --keep-system <系统名称或UUID>
    # 执行
    python -m scripts.cleanup_test_leftovers --keep-system <系统名称或UUID> --execute
    # 多 system + 项目组合保留
    python -m scripts.cleanup_test_leftovers \\
        --keep-system <系统名称或UUID> --keep-system <另一系统> \\
        --keep-project <project-uuid> --execute

安全护栏：
- --keep-system 未指定或解析不到 → 直接报错退出（宁拒勿误删）
- --execute 前须先 dry-run 人工核对数字
- 执行后校验：保留域四项计数（节点/向量/审计/图顶点）与执行前完全一致，
  不一致立即非零退出
- 删除谓词全部显式处理 NULL 三值逻辑（system_id/project_id 可空）
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from collections import Counter

from sqlalchemy import and_, delete, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.approval.models import ApprovalBatch
from mem_lake.audit.models import AuditLog
from mem_lake.db.session import AsyncSessionLocal
from mem_lake.gateway.models import ReindexTask
from mem_lake.knowledge.age_store import get_graph_store
from mem_lake.knowledge.models import (
    KnowledgeNode,
    NodeEmbedding,
    RequirementCounter,
    System,
    SystemProject,
)

# AGE cypher IN 列表单次绑定分片规模（批次二尖刺验证 1000 元素可行）
_CHUNK = 500


def _not_in(col, ids: set[uuid.UUID]):
    """列不在 ids 中（含 NULL 语义：NULL 视为「不在」，参与删除判定）。

    直接用 notin_ 会因 SQL 三值逻辑漏掉 NULL 行（system_id/project_id
    均可空）；空集合返回永真条件（该维度无保留对象）。
    """
    if not ids:
        return true()
    return or_(col.is_(None), col.notin_(ids))


def _keep_node_ids_stmt(keep_system_ids: dict[uuid.UUID, str]):
    """保留 system 下的全部节点 id 子查询（快照与保护集共用同一口径）。"""
    return select(KnowledgeNode.id).where(
        KnowledgeNode.system_id.in_(list(keep_system_ids))
    )


def _node_delete_cond(
    keep_system_ids: dict[uuid.UUID, str], keep_project_ids: set[uuid.UUID]
):
    """节点删除判定：system 不保留 **且** 项目不保留（NULL 按不保留处理）。

    保留 = (system ∈ 保留) 或 (project ∈ 保留)；删除即其补集，两个维度
    都是 _not_in 语义（NULL 视为不在保留集）。
    """
    return and_(
        _not_in(KnowledgeNode.system_id, set(keep_system_ids)),
        _not_in(KnowledgeNode.project_id, keep_project_ids),
    )


async def _resolve_keep_systems(
    session: AsyncSession, specs: list[str]
) -> dict[uuid.UUID, str]:
    """按名称/UUID 解析保留 system；返回 {id: name}。解析不到即抛错。"""
    resolved: dict[uuid.UUID, str] = {}
    systems = list((await session.execute(select(System))).scalars().all())
    for spec in specs:
        hit = next((s for s in systems if s.name == spec), None)
        if hit is None:
            try:
                sid = uuid.UUID(spec)
            except ValueError:
                hit = None
            else:
                hit = next((s for s in systems if s.id == sid), None)
        if hit is None:
            available = ", ".join(sorted(s.name for s in systems)) or "（空库）"
            raise SystemExit(
                f"错误: --keep-system 解析不到 {spec!r}；现存 system: {available}"
            )
        resolved[hit.id] = hit.name
    return resolved


async def _keep_snapshot(
    session: AsyncSession, keep_system_ids: dict[uuid.UUID, str]
) -> dict[str, int]:
    """保留域快照：各 system 的节点/向量/审计数 + 图顶点总数（前后须一致）。"""
    snapshot: dict[str, int] = {}
    keep_node_ids = _keep_node_ids_stmt(keep_system_ids)
    for sid, name in keep_system_ids.items():
        nodes = await session.scalar(
            select(func.count())
            .select_from(KnowledgeNode)
            .where(KnowledgeNode.system_id == sid)
        )
        embeddings = await session.scalar(
            select(func.count())
            .select_from(NodeEmbedding)
            .where(NodeEmbedding.node_id.in_(keep_node_ids))
        )
        audits = await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.target_id.in_(keep_node_ids))
        )
        snapshot[f"{name}:nodes"] = nodes or 0
        snapshot[f"{name}:embeddings"] = embeddings or 0
        snapshot[f"{name}:audits"] = audits or 0
    graph_store = get_graph_store()
    rows = await graph_store._exec_cypher(
        session,
        "MATCH (n) WHERE n.system_id IN $sids RETURN n.id",
        {"sids": [str(s) for s in keep_system_ids]},
    )
    snapshot["graph_vertices"] = len(rows)
    return snapshot


async def _plan(
    session: AsyncSession,
    keep_system_ids: dict[uuid.UUID, str],
    keep_project_ids: set[uuid.UUID],
):
    """清点待删对象。节点判定：system 不保留 且 项目不保留（NULL 按不保留处理）。"""
    node_cond = _node_delete_cond(keep_system_ids, keep_project_ids)
    del_nodes = list(
        (await session.execute(select(KnowledgeNode).where(node_cond)))
        .scalars()
        .all()
    )
    del_systems = list(
        (
            await session.execute(
                select(System).where(System.id.notin_(set(keep_system_ids)))
            )
        )
        .scalars()
        .all()
    )
    del_sys_projects = list(
        (
            await session.execute(
                select(SystemProject).where(SystemProject.system_id.notin_(set(keep_system_ids)))
            )
        )
        .scalars()
        .all()
    )
    del_counters = list(
        (
            await session.execute(
                select(RequirementCounter).where(
                    RequirementCounter.system_id.notin_(set(keep_system_ids))
                )
            )
        )
        .scalars()
        .all()
    )
    # 存活项目 = 保留 system 节点的项目集合（这些项目即使未显式保留也不删其批次）
    surviving_projects = {
        pid
        for (pid,) in (
            await session.execute(
                select(KnowledgeNode.project_id).where(
                    KnowledgeNode.system_id.in_(keep_system_ids)
                )
            )
        ).all()
        if pid is not None
    }
    protected_projects = keep_project_ids | surviving_projects
    del_batches = list(
        (
            await session.execute(
                select(ApprovalBatch).where(
                    _not_in(ApprovalBatch.project_id, protected_projects)
                )
            )
        )
        .scalars()
        .all()
    )
    # 重索引任务：不引用任何受保护节点/项目的行才删
    protected_ids = set(
        (
            await session.execute(_keep_node_ids_stmt(keep_system_ids))
        ).scalars().all()
    )
    del_tasks: list[ReindexTask] = []
    for task in (await session.execute(select(ReindexTask))).scalars().all():
        if task.project_id is not None and task.project_id in protected_projects:
            continue
        if task.target_node_ids and (set(task.target_node_ids) & protected_ids):
            continue
        del_tasks.append(task)
    return (
        del_nodes,
        del_systems,
        del_sys_projects,
        del_counters,
        del_batches,
        del_tasks,
    )


async def _delete_graph_vertices(
    session: AsyncSession, graph_store, node_ids: list[uuid.UUID]
) -> int:
    """分片 DETACH DELETE（连带清边）。"""
    deleted = 0
    for start in range(0, len(node_ids), _CHUNK):
        chunk = [str(nid) for nid in node_ids[start : start + _CHUNK]]
        await graph_store._exec_cypher(
            session,
            "MATCH (n) WHERE n.id IN $ids DETACH DELETE n",
            {"ids": chunk},
        )
        deleted += len(chunk)
    return deleted


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="测试残留清理（默认 dry-run；保留白名单外的知识数据全删）"
    )
    parser.add_argument(
        "--keep-system",
        action="append",
        default=[],
        metavar="NAME_OR_UUID",
        help="保留的 system 域（名称或 UUID，可多次；至少一个）",
    )
    parser.add_argument(
        "--keep-project",
        action="append",
        default=[],
        metavar="UUID",
        help="额外保留的项目域（UUID，可多次）",
    )
    parser.add_argument("--execute", action="store_true", help="实际执行删除（默认只清点）")
    args = parser.parse_args()

    if not args.keep_system:
        raise SystemExit(
            "错误: 必须至少指定一个 --keep-system（名称或 UUID）；"
            "可用 docker exec <pg容器> psql -U <user> -d <db> "
            '-c \'SELECT id, name FROM "system"\' 查看现存 system'
        )

    keep_project_ids: set[uuid.UUID] = set()
    for spec in args.keep_project:
        try:
            keep_project_ids.add(uuid.UUID(spec))
        except ValueError:
            raise SystemExit(f"错误: --keep-project 不是合法 UUID: {spec!r}") from None

    graph_store = get_graph_store()
    async with AsyncSessionLocal() as session:
        keep_system_ids = await _resolve_keep_systems(session, args.keep_system)
        print(
            f"保留 system: {keep_system_ids}；额外保留项目: "
            f"{sorted(str(p) for p in keep_project_ids) or '无'}"
        )

        before = await _keep_snapshot(session, keep_system_ids)
        (
            del_nodes,
            del_systems,
            del_sys_projects,
            del_counters,
            del_batches,
            del_tasks,
        ) = await _plan(session, keep_system_ids, keep_project_ids)

        by_type = Counter(n.type for n in del_nodes)
        orphan_embeddings = await session.scalar(
            select(func.count())
            .select_from(NodeEmbedding)
            .where(~NodeEmbedding.node_id.in_(select(KnowledgeNode.id)))
        )
        print(f"保留域快照（执行前）: {before}")
        print(f"待删节点 {len(del_nodes)} 个（按类型: {dict(by_type)}）")
        print(
            f"待删向量 {len(del_nodes)} 节点的 facet 行 + 孤儿 {orphan_embeddings} 行；"
            f"待删 System {len(del_systems)} / SystemProject {len(del_sys_projects)} / "
            f"计数器 {len(del_counters)} / 批次 {len(del_batches)}（item 级联）/ "
            f"重索引任务 {len(del_tasks)}"
        )
        print("audit_log 与 access_key 不动。")

        if not args.execute:
            print("\n[dry-run] 未执行。确认清单后加 --execute。")
            return

        # ===== 执行 =====
        node_ids = [n.id for n in del_nodes]
        # 1. AGE 图顶点（分片 DETACH DELETE）
        graph_deleted = await _delete_graph_vertices(session, graph_store, node_ids)
        # 2. 向量（节点 facet + 孤儿行）→ 批次（item 级联）→ 重索引 → 节点行
        if node_ids:
            await session.execute(
                delete(NodeEmbedding).where(NodeEmbedding.node_id.in_(node_ids))
            )
        await session.execute(
            delete(NodeEmbedding).where(
                ~NodeEmbedding.node_id.in_(select(KnowledgeNode.id))
            )
        )
        batch_ids = [b.id for b in del_batches]
        if batch_ids:
            await session.execute(
                delete(ApprovalBatch).where(ApprovalBatch.id.in_(batch_ids))
            )
        task_ids = [t.id for t in del_tasks]
        if task_ids:
            await session.execute(
                delete(ReindexTask).where(ReindexTask.id.in_(task_ids))
            )
        if node_ids:
            await session.execute(
                delete(KnowledgeNode).where(KnowledgeNode.id.in_(node_ids))
            )
        # 3. 测试 System 域
        sys_ids = [s.id for s in del_systems]
        if del_sys_projects:
            await session.execute(
                delete(SystemProject).where(
                    SystemProject.system_id.notin_(set(keep_system_ids))
                )
            )
        if del_counters:
            await session.execute(
                delete(RequirementCounter).where(
                    RequirementCounter.system_id.notin_(set(keep_system_ids))
                )
            )
        await session.execute(delete(System).where(System.id.notin_(set(keep_system_ids))))
        await session.commit()

        # 4. 复核：保留域四项计数一致 + 无新增悬空
        after = await _keep_snapshot(session, keep_system_ids)
        dangling_nodes = await session.scalar(
            select(func.count())
            .select_from(KnowledgeNode)
            .where(
                KnowledgeNode.system_id.is_not(None),
                ~KnowledgeNode.system_id.in_(select(System.id)),
            )
        )
        dangling_embeddings = await session.scalar(
            select(func.count())
            .select_from(NodeEmbedding)
            .where(~NodeEmbedding.node_id.in_(select(KnowledgeNode.id)))
        )
        remain_systems = await session.scalar(
            select(func.count())
            .select_from(System)
            .where(System.id.notin_(set(keep_system_ids)))
        )

        print(
            f"\n已执行：删节点 {len(node_ids)} / 图顶点分片 {graph_deleted} / "
            f"System {len(sys_ids)} / 批次 {len(batch_ids)} / 重索引 {len(task_ids)}"
        )
        print(f"保留域快照（执行后）: {after}")
        ok = before == after
        print(
            f"复核：残留 System {remain_systems}（应 0）/ 悬空 system 引用 {dangling_nodes} / "
            f"悬空 embedding {dangling_embeddings}（应 0）/ 保留域一致: {'是' if ok else '否！'}"
        )
        if not ok:
            raise SystemExit("保留域计数与执行前不一致，请立即排查！")


if __name__ == "__main__":
    asyncio.run(main())
