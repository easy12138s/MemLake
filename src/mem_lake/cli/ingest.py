"""批量导入需求文档：存量需求迁移工具（免审批直写路径）。

本 CLI 定位为存量需求的一站式迁移工具：把历史需求文档批量灌入图谱，
供后续演进过程中由审批流维护增量。与 MCP 写工具路径不同：

- 免审批直写：复用 repository.batch_insert_requirements 直接写入
  （status=approved、带向量），不走审批工作流。
- 重复防护：source_doc 幂等——project+system 范围内匹配
  properties.source_doc，命中跳过；同一运行内重复 source_doc 也跳过（先到先得），
  据此实现断点续跑（每 batch_size 切片 commit，已提交批次在崩溃后保留）。
  source_doc 粒度由适配器决定（notes 适配器拆分条目为「文件#N」条目级键）。
- 拆分链边：rel_path 带 #N 后缀的条目视为同文件拆分组，导入完成后按条目序
  补建 relates_to 顺序链边（幂等：先查后建，重跑不重复）。
- 冲突检测：批量直写本身不触发 L0-L3 四层冲突检测；迁移后如需判重，由检索侧
  （check_requirement_conflicts 等）按需发起。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.cli.adapters import get_adapter
from mem_lake.cli.extractor import ParsedRequirement, extract_directory
from mem_lake.knowledge.models import KnowledgeNode, System
from mem_lake.knowledge.repository import (
    add_edge,
    batch_insert_requirements,
    get_system_by_code,
    get_system_by_name,
)

# 拆分条目的 rel_path 后缀（notes 适配器产物）：{文件 rel_path}#{序号}
_SPLIT_SUFFIX_RE = re.compile(r"^(?P<base>.+)#(?P<idx>\d+)$")
# 同文件拆分条目间的链边类型（沿用现有 12 种边类型，不新增）
_CHAIN_EDGE_TYPE = "relates_to"


@dataclass
class ImportSummary:
    """汇总：created/skipped/failed 的文件 rel_path 列表 + pending(dry-run 待导入明细)。

    edges_created：拆分文件（rel_path 带 #N 后缀条目）补建的 relates_to 链边数。
    """

    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    pending: list[ParsedRequirement] = field(default_factory=list)
    edges_created: int = 0


async def resolve_system(
    session: AsyncSession, *, code: str | None = None, name: str | None = None
) -> System:
    """按 code 或 name 解析 System。name 优先，次之 code；均未命中抛 ValueError。

    DB 存量 system 的 code 可能为 NULL（如『中方诊药云系统』），此时用 name 匹配。
    """
    if name is not None:
        sys_obj = await get_system_by_name(session, name)
        if sys_obj is not None:
            return sys_obj
    if code is not None:
        sys_obj = await get_system_by_code(session, code)
        if sys_obj is not None:
            return sys_obj
    raise ValueError(
        f"system 未命中（name={name!r} code={code!r}）——请用 --system-name 或 "
        "--system-code 指定，或先用 manage_system 创建并设置 code"
    )


async def _load_existing_source_docs(
    session: AsyncSession,
    project_id: uuid.UUID | None,
    system_id: uuid.UUID | None,
) -> set[str]:
    """返回 project+system 范围内已存在的 Requirement source_doc 集合。

    范围按 project 判别式与 system_id 双维度限定（system 归属可能跨 project/时间变化）：
    project_id=None 只匹配 project_id IS NULL（悬浮），否则匹配该 project。
    过滤 type=='Requirement' 且 properties.source_doc 非空。单次查询。
    """
    rows = (
        await session.execute(
            select(KnowledgeNode).where(
                KnowledgeNode.type == "Requirement",
                KnowledgeNode.project_id == project_id,
                KnowledgeNode.system_id == system_id,
                KnowledgeNode.is_deleted == False,  # noqa: E712
            )
        )
    ).scalars().all()
    docs: set[str] = set()
    for row in rows:
        source_doc = (row.properties or {}).get("source_doc")
        if source_doc:
            docs.add(source_doc)
    return docs


async def run_import_batch(
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None = None,
    system: System | None = None,
    directory: str,
    adapter: str = "markdown",
    priority: str = "P3",
    module: str = "导入",
    embedding_client: Any = None,
    graph_store: Any = None,
    created_by: str = "cli-import",
    batch_size: int = 50,
    parsed: list[ParsedRequirement] | None = None,
) -> ImportSummary:
    """批量入库驱动：一次性 embed/分配主键/建图/审计，每批 commit。

    - 文件发现/解析：parsed 未传时用 extract_directory（同 CLI 入口的排序与
      source_doc=posix 相对路径规则）；入口已解析时直接传入 parsed 避免重复解析。
    - source_doc 幂等：先查 project+system 范围内已存在集合，命中跳过；
      同一运行内重复 source_doc 也跳过（先到先得）。
    - 每 batch_size 切片一次 batch_insert_requirements + session.commit：
      已提交批次在崩溃后保留，下次运行靠 source_doc 去重跳过，实现断点续跑。
    - system 给定 → allocate_requirement_key=True 分配 HIS-0001；悬浮且无 system 不分配。
    """
    adapter_obj = get_adapter(adapter) if parsed is None else None
    parsed_list = parsed if parsed is not None else extract_directory(directory, adapter=adapter_obj)

    system_id = system.id if system is not None else None
    existing = await _load_existing_source_docs(session, project_id, system_id)
    allocate_requirement_key = system is not None

    summary = ImportSummary()
    seen: set[str] = set()
    nodes: list[KnowledgeNode] = []
    planned: list[str] = []

    for item in parsed_list:
        source_doc = item.rel_path
        if source_doc in existing or source_doc in seen:
            summary.skipped.append(source_doc)
            continue
        seen.add(source_doc)
        nodes.append(
            KnowledgeNode(
                project_id=project_id,
                system_id=system_id,
                requirement_key=None,
                type="Requirement",
                title=item.title,
                content=item.content,
                properties={
                    "priority": priority,
                    "module": module,
                    "source_doc": source_doc,
                },
                tags=[],
                source={"kind": "file", "path": source_doc, "importer": "cli"},
                status="approved",
                version=1,
                created_by=created_by,
            )
        )
        planned.append(source_doc)

    total_batches = (len(nodes) + batch_size - 1) // batch_size
    total_planned = len(planned)
    for idx, start in enumerate(range(0, len(nodes), batch_size), 1):
        chunk = nodes[start : start + batch_size]
        rel_paths = planned[start : start + batch_size]
        try:
            await batch_insert_requirements(
                session,
                graph_store=graph_store,
                embedding_client=embedding_client,
                nodes=chunk,
                actor=created_by,
                allocate_requirement_key=allocate_requirement_key,
                system_id=system_id if allocate_requirement_key else None,
            )
            await session.commit()
        except Exception as exc:
            # 失败批次必须 rollback：不 rollback 时本批已 flush 的行会留在未提交事务里，
            # 被下一个成功批次的 commit 一并落库（缺向量/图/审计的残缺节点）。
            await session.rollback()
            summary.failed.extend(rel_paths)
            print(
                f"[失败] 批次 {idx}/{total_batches}: "
                f"{type(exc).__name__}: {exc}"
            )
            continue
        summary.created.extend(rel_paths)
        print(
            f"[进度] 批次 {idx}/{total_batches}: "
            f"本批 {len(rel_paths)}，累计新增 {len(summary.created)}/{total_planned}"
        )

    summary.edges_created = await _link_split_groups(
        session,
        parsed_list=parsed_list,
        project_id=project_id,
        system_id=system_id,
        graph_store=graph_store,
        actor=created_by,
    )

    return summary


async def _link_split_groups(
    session: AsyncSession,
    *,
    parsed_list: list[ParsedRequirement],
    project_id: uuid.UUID | None,
    system_id: uuid.UUID | None,
    graph_store: Any,
    actor: str,
) -> int:
    """为同一需求文件拆分出的条目补建 relates_to 顺序链边（幂等，独立 commit）。

    - 分组：rel_path 去掉 #{N} 后缀（拆分条目的条目级幂等键由适配器写入）；
      仅处理 ≥2 条目的组（恰好 1 条 = 未拆分文件或回落单条，不建边）
    - 节点定位：项目+system 范围内按条目 source_doc 精确键一次查询，
      断点续跑/重跑时已存在的条目同样入链
    - 幂等：建边前查 from 节点 relates_to 深度 1 邻居，已存在则跳过
      （AGE CREATE 无 MERGE，先查防重跑产生重复边）
    - 失败降级：建边 pass 异常只打印告警并 rollback 本 pass，不回滚已入库节点
    """
    groups: dict[str, dict[int, str]] = {}
    for item in parsed_list:
        m = _SPLIT_SUFFIX_RE.match(item.rel_path)
        if m:
            groups.setdefault(m.group("base"), {})[int(m.group("idx"))] = item.rel_path
    groups = {base: members for base, members in groups.items() if len(members) >= 2}
    if not groups:
        return 0

    rows = (
        await session.execute(
            select(KnowledgeNode).where(
                KnowledgeNode.type == "Requirement",
                KnowledgeNode.project_id == project_id,
                KnowledgeNode.system_id == system_id,
                KnowledgeNode.is_deleted == False,  # noqa: E712
            )
        )
    ).scalars().all()
    node_by_source_doc = {
        doc: row
        for row in rows
        if (doc := (row.properties or {}).get("source_doc"))
    }

    try:
        created = 0
        for members in groups.values():
            chain = [
                node_by_source_doc[members[idx]]
                for idx in sorted(members)
                if members[idx] in node_by_source_doc
            ]
            for first, second in zip(chain, chain[1:]):
                neighbors = await graph_store.neighbors(
                    session, first.id, edge_type=_CHAIN_EDGE_TYPE, depth=1
                )
                neighbor_ids = {
                    (n.get("properties") or {}).get("id") for n in neighbors
                }
                if str(second.id) in neighbor_ids:
                    continue
                await add_edge(
                    session,
                    graph_store=graph_store,
                    from_id=first.id,
                    to_id=second.id,
                    edge_type=_CHAIN_EDGE_TYPE,
                    actor=actor,
                )
                created += 1
        await session.commit()
        return created
    except Exception as exc:  # noqa: BLE001 - 建边失败不阻断导入（节点已入库）
        await session.rollback()
        print(f"[警告] 拆分链边补全失败: {type(exc).__name__}: {exc}")
        return 0
