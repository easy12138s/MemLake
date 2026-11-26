"""测试残留数据清理脚本（一次性运维操作，非常驻代码）。

背景：共享 dev DB 长期被 e2e/集成测试污染（随机 system/project、mock
向量、悬空引用）。本脚本按「保留白名单」口径清点/删除白名单外的一切：
删除范围 = 既不归属保留 system 域、也不归属保留项目的全部知识数据及其
关联行（向量 / AGE 图顶点 / system 域行 / 审批批次 / 重索引任务 / 孤儿向量）。

固定口径（不提供开关，调整时改本常量并评审）：
- audit_log：一律保留（append-only 契约；操作历史有溯源价值）
- access_key：一律不动（无凭据判断依据，宁留勿删）
- embedding_state / EmbeddingState：模型签名单例，不动
- 批次保护：project_id 维度 + **节点归属维度**（审批项命中保留域节点的批次保留，
  避免真实悬浮需求批次被误删——2026-11-26 加固）

保留白名单（CLI 指定，至少一个 system）：
- --keep-system <名称或UUID>：可多次；name 优先（存量 system 常无 code）
- --keep-project <UUID>：可多次（项目域保留；项目无名称字段，仅支持 UUID）
- --drop-project <UUID>：可多次；**显式声明要删除**的项目（仅对「保留 system
  关联项目」有意义，见下方守卫）

守卫（2026-11-26 加固，源于 11-20 误删事故）：
- **保留 system 的关联项目默认受保护**。system↔project 映射（system_project 表）
  是「该项目属于该系统域」的权威口径：其下节点一律不删，除非用 --drop-project
  显式点名。事故复盘：11-20 的清理只列了 --keep-project 7e81667e/0abd0149，
  而「中方诊药云系统」关联的 01276605/6d50c0c9/70e9a3e0 未列 → 删除谓词
  （system∉保留 AND project∉保留）把这三个真实项目的全部 project-scoped
  节点（真实研发资产 64 个 / 111 条边）连锅端，需求（system-owned）存活、
  研发资产全灭。本守卫使该类事故在执行前即失效。
- --drop-project 只放宽「system 关联项目」这一层保护，不放宽 batch/node
  维度之外的任何判定；误点名仍需人工复核 dry-run 清单。

用法（宿主机 venv 或任意能连 DB+AGE 的环境）：
    # dry-run：只清点（默认）
    python -m scripts.cleanup_test_leftovers --keep-system <系统名称或UUID>
    # 执行
    python -m scripts.cleanup_test_leftovers --keep-system <系统名称或UUID> --execute
    # 多 system + 项目组合保留
    python -m scripts.cleanup_test_leftovers \\
        --keep-system <系统名称或UUID> --keep-system <另一系统> \\
        --keep-project <project-uuid> --execute
    # 显式删除某个 system 关联项目（如测试项目）
    python -m scripts.cleanup_test_leftovers \\
        --keep-system <系统名称或UUID> --drop-project <project-uuid> --execute

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

from sqlalchemy import String, and_, cast, delete, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.approval.models import ApprovalBatch, ApprovalItem
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


def _keep_node_ids_stmt(
    keep_system_ids: dict[uuid.UUID, str],
    protected_project_ids: set[uuid.UUID] | None = None,
):
    """保留域节点 id 子查询（system ∪ 受保护项目；快照与保护集共用同一口径）。"""
    conditions = [KnowledgeNode.system_id.in_(list(keep_system_ids))]
    if protected_project_ids:
        conditions.append(KnowledgeNode.project_id.in_(protected_project_ids))
    return select(KnowledgeNode.id).where(or_(*conditions))


def _batch_delete_cond(protected_projects, protected_node_ids):
    """批次删除判定（保守口径）。

    保护规则（任一命中即保留）：
    - project_id 属于保留项目（NULL 视为不保留，但下面第二条兜住悬浮批次）
    - status=pending_review：待审批批次一律不删（target_id 尚未回填）
    - project_id IS NULL：悬浮批次一律不删（宁留勿删）
    - 审批项 target_id 命中保留域节点
    - 审批项 payload 的 node_id/from_ref/to_ref 命中保留域节点（边批次无 target_id）
    """
    kept_node_ids_text = select(cast(KnowledgeNode.id, String)).where(
        KnowledgeNode.id.in_(protected_node_ids)
    )
    touching_target = select(ApprovalItem.batch_id).where(
        ApprovalItem.target_id.in_(protected_node_ids)
    )
    touching_payload = select(ApprovalItem.batch_id).where(
        or_(
            ApprovalItem.payload["node_id"].astext.in_(kept_node_ids_text),
            ApprovalItem.payload["from_ref"].astext.in_(kept_node_ids_text),
            ApprovalItem.payload["to_ref"].astext.in_(kept_node_ids_text),
        )
    )
    return and_(
        _not_in(ApprovalBatch.project_id, protected_projects),
        ApprovalBatch.status != "pending_review",
        ApprovalBatch.project_id.is_not(None),
        ~ApprovalBatch.id.in_(touching_target),
        ~ApprovalBatch.id.in_(touching_payload),
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


def _effective_protected_projects(
    keep_project_ids: set[uuid.UUID],
    system_projects: set[uuid.UUID],
    drop_project_ids: set[uuid.UUID],
) -> set[uuid.UUID]:
    """算出受保护项目集合（节点与批次共用同一口径）。

    - keep_project_ids：--keep-project 显式保留，**优先级最高**（即便同时出现在
      --drop-project 也仍然保护——矛盾输入取安全侧）
    - system_projects：保留 system 的关联项目，默认保护（守卫，2026-11-26）
    - drop_project_ids：--drop-project 显式点名删除，只放开 system_projects 层
    """
    return keep_project_ids | (system_projects - drop_project_ids)


async def _resolve_system_projects(
    session: AsyncSession, keep_system_ids: dict[uuid.UUID, str]
) -> set[uuid.UUID]:
    """保留 system 关联的全部项目（system_project 映射 = 项目归属的权威口径）。

    2026-11-26 加固（源于 11-20 误删事故）：这些项目未列入 --keep-project 时，
    其 project-scoped 节点（真实研发资产）会被删除谓词连锅端。默认纳入保护集，
    只有 --drop-project 显式点名才放开。
    """
    rows = (
        await session.execute(
            select(SystemProject.project_id).where(
                SystemProject.system_id.in_(list(keep_system_ids))
            )
        )
    ).all()
    return {pid for (pid,) in rows if pid is not None}


async def _keep_snapshot(
    session: AsyncSession,
    keep_system_ids: dict[uuid.UUID, str],
    protected_project_ids: set[uuid.UUID] | None = None,
) -> dict[str, int]:
    """保留域快照：各 system 的节点/向量/审计数 + 受保护项目资产 + 图顶点总数。

    前后须一致；受保护项目（system_project 映射默认保护）也必须纳入，
    否则本次守卫兜住的资产被删时快照看不出来。
    """
    snapshot: dict[str, int] = {}
    keep_node_ids = _keep_node_ids_stmt(keep_system_ids, protected_project_ids)
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
    if protected_project_ids:
        snapshot["protected_project_nodes"] = (
            await session.scalar(
                select(func.count())
                .select_from(KnowledgeNode)
                .where(KnowledgeNode.project_id.in_(protected_project_ids))
            )
            or 0
        )
    graph_store = get_graph_store()
    rows = await graph_store._exec_cypher(
        session,
        "MATCH (n) "
        "WHERE n.system_id IN $sids OR n.project_id IN $pids "
        "RETURN n.id",
        {
            "sids": [str(s) for s in keep_system_ids],
            "pids": [str(p) for p in (protected_project_ids or set())],
        },
    )
    snapshot["graph_vertices"] = len(rows)
    return snapshot


async def _plan(
    session: AsyncSession,
    keep_system_ids: dict[uuid.UUID, str],
    keep_project_ids: set[uuid.UUID],
    drop_project_ids: set[uuid.UUID] | None = None,
):
    """清点待删对象。节点判定：system 不保留 且 项目不保留（NULL 按不保留处理）。

    保护集（节点与批次共用同一口径）：
    - keep_project_ids：--keep-project 显式保留
    - system_projects：**保留 system 的关联项目默认保护**（守卫，2026-11-26）
    - drop_project_ids：--drop-project 显式点名删除，从上述两层保护中扣除
    """
    drop_project_ids = drop_project_ids or set()
    system_projects = await _resolve_system_projects(session, keep_system_ids)
    protected_project_ids = _effective_protected_projects(
        keep_project_ids, system_projects, drop_project_ids
    )
    node_cond = _node_delete_cond(keep_system_ids, protected_project_ids)
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
    protected_projects = protected_project_ids | surviving_projects
    # 批次保护补强（2026-11-26 加固）：project_id 为空的批次（悬浮需求批次）按
    # project 维度无法判定归属，原口径会把「审批项指向保留域节点」的真实悬浮批次
    # 一并删掉（实测 55 个真实批次 / 68 个审批项）。补一条按节点归属的保护：
    # 审批项 target_id 命中保留域节点（system ∈ 保留 或 project ∈ 保留）的批次保留。
    protected_node_ids = select(KnowledgeNode.id).where(
        or_(
            KnowledgeNode.system_id.in_(list(keep_system_ids)),
            KnowledgeNode.project_id.in_(list(protected_project_ids)),
        )
    )
    del_batches = list(
        (
            await session.execute(
                select(ApprovalBatch).where(
                    _batch_delete_cond(protected_projects, protected_node_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    # 重索引任务：不引用任何受保护节点/项目的行才删
    protected_ids = set(
        (
            await session.execute(
                _keep_node_ids_stmt(keep_system_ids, protected_project_ids)
            )
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
        system_projects,
        protected_project_ids,
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
    parser.add_argument(
        "--drop-project",
        action="append",
        default=[],
        metavar="UUID",
        help=(
            "显式声明要删除的项目（UUID，可多次）；仅用于放开「保留 system 关联"
            "项目」的默认保护，不影响其它判定"
        ),
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

    drop_project_ids: set[uuid.UUID] = set()
    for spec in args.drop_project:
        try:
            drop_project_ids.add(uuid.UUID(spec))
        except ValueError:
            raise SystemExit(f"错误: --drop-project 不是合法 UUID: {spec!r}") from None

    graph_store = get_graph_store()
    async with AsyncSessionLocal() as session:
        keep_system_ids = await _resolve_keep_systems(session, args.keep_system)
        print(
            f"保留 system: {keep_system_ids}；额外保留项目: "
            f"{sorted(str(p) for p in keep_project_ids) or '无'}"
        )

        (
            del_nodes,
            del_systems,
            del_sys_projects,
            del_counters,
            del_batches,
            del_tasks,
            system_projects,
            protected_project_ids,
        ) = await _plan(
            session,
            keep_system_ids,
            keep_project_ids,
            drop_project_ids,
        )
        before = await _keep_snapshot(
            session, keep_system_ids, protected_project_ids
        )

        implicit = sorted(str(p) for p in system_projects - keep_project_ids)
        dropped = sorted(str(p) for p in drop_project_ids)
        print(
            "守卫：保留 system 的关联项目默认受保护 —— "
            f"受保护项目 {len(protected_project_ids)} 个；"
            f"其中未显式 --keep-project（靠守卫兜住）{implicit or '无'}；"
            f"--drop-project 显式删除 {dropped or '无'}"
        )
        if dropped and not (set(dropped) & {str(p) for p in system_projects}):
            print(
                "警告: --drop-project 指定的项目不属于任何保留 system（可能拼错或已删），"
                "本次不产生额外影响"
            )
        conflict = sorted(str(p) for p in keep_project_ids & drop_project_ids)
        if conflict:
            print(
                f"警告: 项目同时出现在 --keep-project 与 --drop-project：{conflict}；"
                "按安全侧处理（仍然保留）"
            )

        by_type = Counter(n.type for n in del_nodes)
        pending_batches = sum(
            1 for b in del_batches if b.status == "pending_review"
        )
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
        if pending_batches:
            print(
                f"警告: 待删批次中含 {pending_batches} 个 pending_review —— "
                "不应发生，请先停止并核对批次保护逻辑"
            )

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
        after = await _keep_snapshot(
            session, keep_system_ids, protected_project_ids
        )
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
