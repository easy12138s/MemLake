"""批量导入需求文档：入库驱动。

source_doc（=原始相对路径）幂等：project+system 范围内匹配 properties.source_doc。
未命中 → create_node 新建；命中且无 --force → skip；命中且 --force → update_node 覆盖。
复用 repository.create_node/update_node 直写（带向量、不进审批），与 manage 路径一致。

单文件失败隔离：run_import 对每个 parsed 单独调用 ingest_requirement，捕获异常记入
failed，其余继续。dry_run 只解析并产出 pending 清单，不触 ingest。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.cli.adapters import get_adapter
from mem_lake.cli.extractor import ParsedRequirement, extract_directory
from mem_lake.knowledge.models import KnowledgeNode, System
from mem_lake.knowledge.repository import (
    batch_insert_requirements,
    create_node,
    update_node,
)


@dataclass
class ImportSummary:
    """汇总：created/skipped/failed 的文件 rel_path 列表 + pending(dry-run 待导入明细)。"""

    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    pending: list[ParsedRequirement] = field(default_factory=list)


async def resolve_system(
    session: AsyncSession, *, code: str | None = None, name: str | None = None
) -> System:
    """按 code 或 name 解析 System。name 优先，次之 code；均未命中抛 ValueError。

    DB 存量 system 的 code 可能为 NULL（如『中方诊药云系统』），此时用 name 匹配。
    """
    if name is not None:
        sys_obj = (
            await session.execute(select(System).where(System.name == name))
        ).scalar_one_or_none()
        if sys_obj is not None:
            return sys_obj
    if code is not None:
        sys_obj = (
            await session.execute(select(System).where(System.code == code))
        ).scalar_one_or_none()
        if sys_obj is not None:
            return sys_obj
    raise ValueError(
        f"system 未命中（name={name!r} code={code!r}）——请用 --system-name 或 "
        "--system-code 指定，或先用 manage_system 创建并设置 code"
    )


async def find_requirement_by_source(
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None,
    system_id: uuid.UUID,
    source_doc: str,
) -> KnowledgeNode | None:
    """在 project+system 范围内按 properties.source_doc 匹配已存在 Requirement。"""
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
    for row in rows:
        if (row.properties or {}).get("source_doc") == source_doc:
            return row
    return None


async def ingest_requirement(
    session: AsyncSession,
    *,
    graph_store: Any,
    embedding_client: Any,
    project_id: uuid.UUID | None,
    system_id: uuid.UUID,
    parsed: ParsedRequirement,
    priority: str,
    module: str,
    force: bool,
    actor: str,
    created_by: str,
) -> dict[str, Any]:
    """单个解析结果入库。返回 {"status": "created"|"skipped"|"updated", "title": str}。

    不 commit（由调用方统一提交事务）。
    """
    properties = {
        "priority": priority,
        "module": module,
        "source_doc": parsed.rel_path,
    }
    source = {"kind": "file", "path": parsed.rel_path, "importer": "cli"}

    existing = await find_requirement_by_source(
        session,
        project_id=project_id,
        system_id=system_id,
        source_doc=parsed.rel_path,
    )

    if existing is not None:
        if not force:
            return {"status": "skipped", "title": parsed.title}
        await update_node(
            session,
            graph_store=graph_store,
            embedding_client=embedding_client,
            node_id=existing.id,
            title=parsed.title,
            content=parsed.content,
            properties=properties,
            source=source,
            actor=actor,
            regenerate_vector=True,
        )
        return {"status": "updated", "title": parsed.title}

    await create_node(
        session,
        graph_store=graph_store,
        embedding_client=embedding_client,
        project_id=project_id,
        system_id=system_id,
        node_type="Requirement",
        title=parsed.title,
        content=parsed.content,
        properties=properties,
        source=source,
        created_by=created_by,
        generate_vector=True,
    )
    return {"status": "created", "title": parsed.title}


async def run_import(
    session: AsyncSession,
    *,
    graph_store: Any = None,
    embedding_client: Any = None,
    project_id: uuid.UUID | None,
    system_id: uuid.UUID,
    system_code: str | None,
    parsed_list: list[ParsedRequirement],
    priority: str,
    module: str,
    force: bool,
    dry_run: bool,
    actor: str,
    created_by: str,
    ingest: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> ImportSummary:
    """驱动整批导入。system_code 仅用于日志展示（resolve_system 由入口负责）。

    ingest 可注入 fake（默认 ingest_requirement），便于单测驱动逻辑。
    dry_run=True 时只收集 pending 清单，不调用 ingest。
    """
    summary = ImportSummary()
    target = ingest if ingest is not None else ingest_requirement

    if dry_run:
        summary.pending = list(parsed_list)
        return summary

    for parsed in parsed_list:
        try:
            result = await target(
                session,
                graph_store=graph_store,
                embedding_client=embedding_client,
                project_id=project_id,
                system_id=system_id,
                parsed=parsed,
                priority=priority,
                module=module,
                force=force,
                actor=actor,
                created_by=created_by,
            )
        except Exception as exc:
            summary.failed.append(parsed.rel_path)
            print(f"[失败] {parsed.rel_path}: {type(exc).__name__}: {exc}")
            continue
        status = result.get("status")
        if status in ("created", "updated"):
            summary.created.append(parsed.rel_path)
        else:
            summary.skipped.append(parsed.rel_path)
    return summary


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

    与 run_import 的差异：
    - 文件发现/解析用 extract_directory（同 CLI 入口的排序与 source_doc=posix 相对路径规则）。
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

    for start in range(0, len(nodes), batch_size):
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
            summary.failed.extend(rel_paths)
            print(
                f"[失败] 批次 {start // batch_size + 1}: "
                f"{type(exc).__name__}: {exc}"
            )
            continue
        summary.created.extend(rel_paths)

    return summary
