"""批量导入需求文档 CLI 入口。

容器内运行（连 Postgres/AGE/embedding）：
    docker exec -it deploy-mem-lake-1 memlake-import-requirements /data/reqs \
        --system-code HIS --project <project-id> [--priority P3] [--module 导入] \
        [--force] [--dry-run]

流程：解析参数 → 开 DB session → resolve system（fail-fast）→ extract_directory
→ run_import（dry-run 只出清单）→ 提交事务 → 打印汇总；有失败则非零退出码。
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from mem_lake.cli.extractor import extract_directory
from mem_lake.cli.ingest import resolve_system, run_import
from mem_lake.db.session import AsyncSessionLocal
from mem_lake.embedding.client import get_embedding_client
from mem_lake.knowledge.age_store import get_graph_store


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """argparse 参数解析。错误时由 argparse 直接 exit(2)。"""
    parser = argparse.ArgumentParser(
        prog="memlake-import-requirements",
        description="扫描文件夹中的 HTML 需求文档，批量导入为 Requirement 节点（每文件一个需求）。",
    )
    parser.add_argument("folder", help="需求文档根目录（递归扫描 .html/.htm）")
    parser.add_argument("--system-code", required=True, help="目标 System.code（如 HIS）")
    parser.add_argument("--project", required=True, help="目标 Project UUID")
    parser.add_argument(
        "--priority", default="P3", help="默认优先级（未识别到时使用，默认 P3）"
    )
    parser.add_argument(
        "--module", default="导入", help="默认模块（未识别到时使用，默认 '导入'）"
    )
    parser.add_argument(
        "--force", action="store_true", help="source_doc 已存在时用 update 覆盖"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只解析并打印待导入清单，不写库"
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    """主流程。返回进程退出码（0=成功或 dry-run；1=有失败；2=参数/内置错误）。"""
    args = _parse_args(argv)

    try:
        project_id = uuid.UUID(args.project)
    except ValueError:
        print(f"错误: --project 不是合法 UUID: {args.project!r}")
        return 2

    async with AsyncSessionLocal() as session:
        system = await resolve_system(session, args.system_code)
        system_id = system.id

        parsed_list = extract_directory(args.folder)
        print(f"解析完成: {len(parsed_list)} 个需求文档")

        embedding_client = get_embedding_client() if not args.dry_run else None
        graph_store = get_graph_store() if not args.dry_run else None

        summary = await run_import(
            session,
            graph_store=graph_store,
            embedding_client=embedding_client,
            project_id=project_id,
            system_id=system_id,
            system_code=args.system_code,
            parsed_list=parsed_list,
            priority=args.priority,
            module=args.module,
            force=args.force,
            dry_run=args.dry_run,
            actor="cli-import",
            created_by="cli-import",
        )

        if not args.dry_run:
            await session.commit()

    if args.dry_run:
        print(f"\n[dry-run] 待导入 {len(summary.pending)} 个：")
        for p in summary.pending:
            print(f"  - {p.title}")
        print("未写库（dry-run）。")
        return 0

    print(
        f"\n导入完成: 新增 {len(summary.created)}，跳过 {len(summary.skipped)}，"
        f"失败 {len(summary.failed)}"
    )
    for rel in summary.skipped:
        print(f"  跳过: {rel}（source_doc 已存在，--force 可覆盖）")
    for rel in summary.failed:
        print(f"  失败: {rel}")
    return 1 if summary.failed else 0


def run(argv: list[str] | None = None) -> None:
    """console_scripts 入口（memlake-import-requirements）。"""
    rc = asyncio.run(main(argv))
    raise SystemExit(rc)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
