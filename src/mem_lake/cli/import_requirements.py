"""批量导入需求文档 CLI 入口。

容器内运行（连 Postgres/AGE/embedding）：
    docker exec -it deploy-mem-lake-1 memlake-import-requirements /data/reqs \
        --system-code HIS [--project <project-id>] [--adapter markdown] \
        [--priority P3] [--module 导入] [--force] [--dry-run]

不传 --project 表示导入为悬浮式 Requirement（project_id=None）。
--adapter 支持 markdown（默认）/ axure（Axure HTML 清洗）。
--system-code / --system-name 至少其一；name 优先（DB 存量 system 常 code 为 NULL）。

流程：解析参数 → 开 DB session → resolve system（fail-fast）→ extract_directory
→ run_import（dry-run 只出清单）→ 提交事务 → 打印汇总；有失败则非零退出码。
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from mem_lake.cli.adapters import ADAPTERS, get_adapter
from mem_lake.cli.extractor import extract_directory
from mem_lake.cli.ingest import resolve_system, run_import
from mem_lake.db.session import AsyncSessionLocal
from mem_lake.embedding.client import get_embedding_client
from mem_lake.knowledge.age_store import get_graph_store


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="memlake-import-requirements",
        description="扫描文件夹中的需求文档，批量导入为 Requirement 节点（每文件一个需求）。",
    )
    parser.add_argument("folder", help="需求文档根目录（递归扫描）")
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--system-code", help="目标 System.code（如 HIS）")
    grp.add_argument("--system-name", help="目标 System.name（如 中方诊药云系统）")
    parser.add_argument("--project", help="目标 Project UUID；不传=悬浮需求")
    parser.add_argument(
        "--adapter",
        default="markdown",
        choices=sorted(ADAPTERS),
        help=f"解析适配器（{sorted(ADAPTERS)}；默认 markdown，向后兼容）",
    )
    parser.add_argument("--priority", default="P3", help="默认优先级（默认 P3）")
    parser.add_argument("--module", default="导入", help="默认模块（默认 '导入'）")
    parser.add_argument("--force", action="store_true", help="source_doc 已存在时用 update 覆盖")
    parser.add_argument("--dry-run", action="store_true", help="只解析并打印待导入清单，不写库")
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    project_id: uuid.UUID | None = None
    if args.project:
        try:
            project_id = uuid.UUID(args.project)
        except ValueError:
            print(f"错误: --project 不是合法 UUID: {args.project!r}")
            return 2

    async with AsyncSessionLocal() as session:
        system = await resolve_system(
            session, code=args.system_code, name=args.system_name
        )
        system_id = system.id

        adapter = get_adapter(args.adapter)
        parsed_list = extract_directory(args.folder, adapter=adapter)
        print(
            f"解析完成: {len(parsed_list)} 个需求文档 (adapter={args.adapter}, "
            f"system={system.name!r}{(' project=' + str(project_id)) if project_id else ' 悬浮'})"
        )

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
    raise SystemExit(asyncio.run(main(argv)))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
