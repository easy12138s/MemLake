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

from mem_lake.cli.extractor import ParsedRequirement
from mem_lake.knowledge.models import KnowledgeNode, System
from mem_lake.knowledge.repository import create_node, update_node


@dataclass
class ImportSummary:
    """汇总：created/skipped/failed 的文件 rel_path 列表 + pending(dry-run 待导入明细)。"""

    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    pending: list[ParsedRequirement] = field(default_factory=list)


async def resolve_system(session: AsyncSession, system_code: str) -> System:
    """按 code 解析 System，未命中抛 ValueError（fail-fast）。"""
    sys_obj = (
        await session.execute(select(System).where(System.code == system_code))
    ).scalar_one_or_none()
    if sys_obj is None:
        raise ValueError(
            f"system 不存在: {system_code!r}（请先用 manage_system 创建并设置 code）"
        )
    return sys_obj


async def find_requirement_by_source(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
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
    project_id: uuid.UUID,
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
    project_id: uuid.UUID,
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
