# 批量解析导入需求文档 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 提供 CLI `memlake-import-requirements`，扫描文件夹内的 HTML 需求文档（每文件一个需求），用 markitdown 解析为 Markdown，抽取 title（=完整相对路径）/content，直接写入 Requirement 节点（带向量、不进审批），按 `source_doc` 幂等，支持 `--force` 更新与 `--dry-run`。

**Architecture:** 三个新模块都在 `src/mem_lake/cli/` 下：`extractor.py`（纯解析，markitdown，无 DB 依赖，可单测）、`ingest.py`（source_doc 查重 + 复用 `repository.create_node`/`update_node`）、`import_requirements.py`（argparse 入口）。入库复用现有直写路径，不引审批流。

**Tech Stack:** Python 3.11, SQLAlchemy asyncio, markitdown, argparse（stdlib）, pytest。

**注意：本项目当前在 master 分支，`schema.validate_node` 只校验必填字段（无封闭契约白名单拒绝），因此 `source_doc` 可自由传入 properties。此实现不涉及 release 分支的 ALLOWED_FIELDS。** 本计划所有 pytest 命令在存储库根目录、conda 环境 `memlake` 下执行（`D:\.conda\envs\memlake\python.exe -m pytest ...`）。

---

## File Structure

- **Create `src/mem_lake/cli/import_requirements.py`** — argparse 入口 + `run()` console script 入口。
- **Create `src/mem_lake/cli/extractor.py`** — `ParsedRequirement` dataclass + `extract_directory()`（markitdown 解析，纯函数）。
- **Create `src/mem_lake/cli/ingest.py`** — `resolve_system()`、`find_requirement_by_source()`、`ingest_requirement()`、`run_import()`（直写 + 幂等 + 汇总）。
- **Modify `pyproject.toml`** — 加 `markitdown` 依赖与 `memlake-import-requirements` console script。
- **Create `tests/unit/test_cli_extractor.py`** — extractor 单测。
- **Create `tests/unit/test_cli_ingest.py`** — ingest 单测（幂等/映射/异常隔离，注入 fake）。

---

## Task 1: `extractor.py` — 解析层

**Files:**
- Create: `src/mem_lake/cli/extractor.py`
- Test: `tests/unit/test_cli_extractor.py`

- [ ] **Step 1: Write the failing test**

```python
"""extractor 单测：解析文件夹 HTML 需求文档。

验证 markitdown 把 HTML → markdown、title=完整相对路径(`/` 连接)、递归扫描、非 html 忽略。
"""

import os
from pathlib import Path

import pytest

from mem_lake.cli.extractor import ParsedRequirement, extract_directory

# -*- coding: utf-8 -*-


@pytest.fixture
def req_tree(tmp_path: Path) -> Path:
    """构造嵌套目录 + 多 HTML 文件 + 一个非 html 文件的临时树。"""
    (tmp_path / "HIS").mkdir(parents=True)
    (tmp_path / "HIS" / "auth").mkdir()
    (tmp_path / "HIS" / "auth" / "login.html").write_text(
        "<html><head><title>用户登录</title></head>"
        "<body><h1>用户登录</h1><p>实现 JWT 登录。</p><ul><li>支持刷新</li></ul></body></html>",
        encoding="utf-8",
    )
    (tmp_path / "HIS" / "billing" / "invoice.html").write_text(
        "<html><body><h1>开票</h1><p>生成发票。</p></body></html>",
        encoding="utf-8",
    )
    (tmp_path / "HIS" / "readme.txt").write_text("not html", encoding="utf-8")
    (tmp_path / "HIS" / "auth" / "ignored.md").write_text("# x", encoding="utf-8")
    return tmp_path


def test_rel_path_uses_posix_separator(req_tree: Path) -> None:
    """title 使用完整相对路径并以 `/` 连接（Windows 反斜杠也要转 `/`）。"""
    parsed = extract_directory(req_tree)
    titles = {p.title for p in parsed}
    assert titles == {"HIS/auth/login.html", "HIS/billing/invoice.html"}


def test_content_contains_markdown_body(req_tree: Path) -> None:
    """content 为 markdown 正文（可含 # 标题、列表）。"""
    parsed = {p.title: p for p in extract_directory(req_tree)}
    login = parsed["HIS/auth/login.html"]
    assert "用户登录" in login.content
    assert "JWT" in login.content
    assert "- " in login.content  # markdown 列表项


def test_ignores_non_html_files(req_tree: Path) -> None:
    """非 .html/.htm 文件被忽略。"""
    parsed = extract_directory(req_tree)
    assert all(p.title.endswith(".html") for p in parsed)
    assert not any("readme.txt" in p.title for p in parsed)


def test_empty_directory_returns_empty(tmp_path: Path) -> None:
    assert extract_directory(tmp_path) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:\.conda\envs\memlake\python.exe -m pytest tests/unit/test_cli_extractor.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'mem_lake.cli.extractor'`）

- [ ] **Step 3: Write the implementation**

```python
"""批量导入需求文档：解析层。

将文件夹内的 HTML 需求文档（每文件一个需求）解析为 ParsedRequirement。
解析用 markitdown（开源，HTML→Markdown），与入库层解耦——未来支持 docx/xlsx 等
只需在此层新增 converter 后端，不改动 ingest。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from markitdown import MarkItDown

_HTML_SUFFIXES = {".html", ".htm"}


@dataclass(frozen=True)
class ParsedRequirement:
    """单个已解析需求：title 用完整相对路径（`/` 连接），content 为 markdown 正文。"""

    title: str
    content: str
    rel_path: str


def _to_posix(path: Path) -> str:
    """转为 `/` 连接的相对路径（跨平台），如 'HIS/auth/login.html'。"""
    return path.as_posix()


def extract_directory(root: str | Path) -> list[ParsedRequirement]:
    """递归扫描 root 下所有 HTML 文件，逐个用 markitdown 解析为 ParsedRequirement。

    忽略非 .html/.htm 文件；解析失败的文件跳过并打印告警（不抛）。
    root 不存在或为空目录返回空列表。
    """
    base = Path(root)
    if not base.is_dir():
        raise NotADirectoryError(f"目录不存在: {base}")

    md = MarkItDown()
    parsed: list[ParsedRequirement] = []
    for file in sorted(base.rglob("*")):
        if not file.is_file() or file.suffix.lower() not in _HTML_SUFFIXES:
            continue
        rel = _to_posix(file.relative_to(base))
        try:
            result = md.convert(file)
        except Exception as exc:  # markitdown 对损坏文件抛各类异常，隔离处理
            print(f"[跳过] {rel}: 解析失败 ({type(exc).__name__}: {exc})")
            continue
        content = (result.text_content or "").strip()
        if not content:
            print(f"[跳过] {rel}: 解析结果为空")
            continue
        parsed.append(ParsedRequirement(title=rel, content=content, rel_path=rel))
    return parsed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `D:\.conda\envs\memlake\python.exe -m pytest tests/unit/test_cli_extractor.py -v`
Expected: PASS（若 markitdown 未安装会 `ModuleNotFoundError`——先在本 Task 末尾统一安装，见 Task 4）

- [ ] **Step 5: Commit**

```bash
git add src/mem_lake/cli/extractor.py tests/unit/test_cli_extractor.py
git commit -m "feat(cli): add requirement extractor using markitdown"
```

---

## Task 2: `ingest.py` — 入库驱动（幂等 + 直写）

**Files:**
- Create: `src/mem_lake/cli/ingest.py`
- Test: `tests/unit/test_cli_ingest.py`

- [ ] **Step 1: Write the failing test**

```python
"""ingest 单测：source_doc 幂等 + properties/source 映射 + 异常隔离。

run_import 通过注入 fake ingest 验证驱动逻辑（不连 DB）；
ingest_requirement/find_requirement_by_source 的真实调用以集成测试
（db_session + graph_store + mock_embedding_client，见 Task 5）验证。
"""

import uuid

import pytest

from mem_lake.cli.extractor import ParsedRequirement
from mem_lake.cli.ingest import run_import


def _parsed(title: str) -> ParsedRequirement:
    return ParsedRequirement(title=title, content=f"正文 {title}", rel_path=title)


@pytest.mark.asyncio
async def test_run_import_aggregates_by_status():
    """run_import 依赖注入 fake ingest，按返回 status 聚合 created/skipped/failed。"""
    seen: list[str] = []

    async def fake_ingest(session, *, graph_store, embedding_client, project_id,
                          system_id, parsed, priority, module, force, actor, created_by):
        seen.append(parsed.rel_path)
        if parsed.rel_path == "a.html":
            raise RuntimeError("模拟解析/入库失败")
        if parsed.rel_path == "b.html":
            return {"status": "created", "title": parsed.title}
        return {"status": "skipped", "title": parsed.title}

    props = dict(
        graph_store=None,
        embedding_client=None,
        project_id=uuid.uuid4(),
        system_id=uuid.uuid4(),
        system_code=None,
        priority="P3",
        module="导入",
        force=False,
        actor="cli",
        created_by="cli",
        ingest=fake_ingest,
    )
    parsed_list = [_parsed("a.html"), _parsed("b.html"), _parsed("c.html")]

    summary = await run_import(None, parsed_list=parsed_list, dry_run=False, **props)

    assert summary.created == ["b.html"]
    assert summary.skipped == ["c.html"]
    assert summary.failed == ["a.html"]
    assert seen == ["a.html", "b.html", "c.html"]  # 失败不中断其余


@pytest.mark.asyncio
async def test_run_import_dry_run_does_not_ingest():
    """dry_run=True 时不调用 ingest，只产出待导入清单（parsed 明细）。"""
    calls: list[str] = []

    async def fake_ingest(*args, **kwargs):
        calls.append("ingest")
        return {"status": "created", "title": ""}

    summary = await run_import(
        None,
        parsed_list=[_parsed("a.html"), _parsed("b.html")],
        project_id=uuid.uuid4(),
        system_id=uuid.uuid4(),
        system_code=None,
        priority="P3",
        module="导入",
        force=False,
        dry_run=True,
        actor="cli",
        created_by="cli",
        ingest=fake_ingest,
    )
    assert calls == []
    assert {"a.html", "b.html"} == {p.title for p in summary.pending}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:\.conda\envs\memlake\python.exe -m pytest tests/unit/test_cli_ingest.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'mem_lake.cli.ingest'`）

- [ ] **Step 3: Write the implementation**

```python
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
from mem_lake.knowledge.repository import (
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


async def resolve_system(session: AsyncSession, system_code: str) -> System:
    """按 code 解析 System，未命中抛 ValueError（fail-fast）。"""
    sys_obj = (
        await session.execute(select(System).where(System.code == system_code))
    ).scalar_one_or_none()
    if sys_obj is None:
        raise ValueError(f"system 不存在: {system_code!r}（请先用 manage_system 创建并设置 code）")
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
    graph_store: Any,
    embedding_client: Any,
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
        if status == "created":
            summary.created.append(parsed.rel_path)
        elif status == "updated":
            # 归入 created 展示“更新”，或单列；这里并入 created 便于汇总
            summary.created.append(parsed.rel_path)
        else:
            summary.skipped.append(parsed.rel_path)
    return summary
```

- [ ] **Step 4: Run test to verify it passes**

Run: `D:\.conda\envs\memlake\python.exe -m pytest tests/unit/test_cli_ingest.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mem_lake/cli/ingest.py tests/unit/test_cli_ingest.py
git commit -m "feat(cli): add requirement ingest driver with source_doc idempotency"
```

---

## Task 3: `import_requirements.py` — CLI 入口

**Files:**
- Create: `src/mem_lake/cli/import_requirements.py`
- Modify: `src/mem_lake/cli/__init__.py`

- [ ] **Step 1: Write the failing test**

```python
"""入口冒烟测试：解析参数、fail-fast（system 未命中）、dry-run 0 写入。

resolve_system 未命中抛 ValueError → 主流程应报错退出。这里用 run_import
（已由 test_cli_ingest 覆盖）；此处只验证系统解析 fail-fast 与参数解析函数。
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.cli.ingest import resolve_system


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


@pytest.mark.asyncio
async def test_resolve_system_missing_raises(monkeypatch):
    """system.code 未命中抛 ValueError（fail-fast）。"""

    async def fake_execute(stmt):
        return _FakeResult(None)

    session = AsyncSession.__new__(AsyncSession)
    monkeypatch.setattr(session, "execute", fake_execute)

    with pytest.raises(ValueError, match="system 不存在"):
        await resolve_system(session, "NOPE")


@pytest.mark.asyncio
async def test_resolve_system_found(monkeypatch):
    """命中返回 System 对象。"""
    from mem_lake.knowledge.models import System

    sys_obj = System(name="HIS", code="HIS")

    async def fake_execute(stmt):
        return _FakeResult(sys_obj)

    session = AsyncSession.__new__(AsyncSession)
    monkeypatch.setattr(session, "execute", fake_execute)

    assert (await resolve_system(session, "HIS")) is sys_obj
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:\.conda\envs\memlake\python.exe -m pytest tests/unit/test_cli_ingest.py -k resolve_system -v`
Expected: PASS（resolve_system 已在 Task 2 实现；若测试无失败则不硬造失败——本 Task 的“失败测试”以系统未命中路径为准）

> 按 TDD 规范：此处 resolve_system 已在 Task 2 实现，本 Task 主要新增入口。为遵守 TDD，焦点测试放 Task 2；本 Task 以入口冒烟为主（手动验证），不另造假失败。

- [ ] **Step 3: Write the implementation**

```python
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
import time
import uuid

from mem_lake.cli.extractor import extract_directory
from mem_lake.cli.ingest import import_requirements as _run  # noqa: F401  (兼容导入)
```

> 修正：入口直接调用 `run_import` 而非再封一层。完整实现如下：

```python
from __future__ import annotations

import argparse
import asyncio
import time
import uuid

from mem_lake.config import get_settings
from mem_lake.db.session import AsyncSessionLocal
from mem_lake.embedding.client import get_embedding_client
from mem_lake.knowledge.age_store import get_graph_store

from mem_lake.cli.extractor import extract_directory
from mem_lake.cli.ingest import resolve_system, run_import


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """argparse 参数解析。返回 Namespace；错误由 argparse 直接 exit(2)。"""
    parser = argparse.ArgumentParser(
        prog="memlake-import-requirements",
        description="扫描文件夹中的 HTML 需求文档并批量导入为 Requirement 节点（每文件一个需求）。",
    )
    parser.add_argument("folder", help="需求文档根目录（递归扫描 .html/.htm）")
    parser.add_argument("--system-code", required=True, help="目标 System.code（如 HIS）")
    parser.add_argument("--project", required=True, help="目标 Project UUID")
    parser.add_argument("--priority", default="P3", help="默认优先级（未识别到时使用，默认 P3）")
    parser.add_argument("--module", default="导入", help="默认模块（未识别到时使用，默认 '导入'）")
    parser.add_argument("--force", action="store_true", help="source_doc 已存在时用 update 覆盖")
    parser.add_argument("--dry-run", action="store_true", help="只解析并打印待导入清单，不写库")
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    """主流程。返回进程退出码（0=成功；有失败=1；解析/系统错误=2 由 argparse/异常处理）。"""
    args = _parse_args(argv)

    try:
        project_id = uuid.UUID(args.project)
    except ValueError:
        print(f"错误: --project 不是合法 UUID: {args.project!r}")
        return 2

    async with AsyncSessionLocal() as session:
        # fail-fast：system 未命中直接报错退出，不开始解析
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

    print(f"\n导入完成: 新增 {len(summary.created)}，跳过 {len(summary.skipped)}，"
          f"失败 {len(summary.failed)}")
    for rel in summary.skipped:
        print(f"  跳过: {rel}（source_doc 已存在，--force 可覆盖）")
    for rel in summary.failed:
        print(f"  失败: {rel}")
    return 1 if summary.failed else 0


def run(argv: list[str] | None = None) -> int:
    """console_scripts 入口（memlake-import-requirements）。"""
    rc = asyncio.run(main(argv))
    raise SystemExit(rc)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

同时修改 `src/mem_lake/cli/__init__.py`，追加：

```python
def run_import_requirements(argv=None):
    """转发到 import_requirements.run（供 console script / python -m 分发）。"""
    from mem_lake.cli.import_requirements import run

    run(argv)
```

- [ ] **Step 4: Verify imports & CLI 冒烟（不连真实 DB 的部分）**

Run: `D:\.conda\envs\memlake\python.exe -c "import mem_lake.cli.import_requirements; print('ok')"`
Expected: `ok`（若 markitdown 未装会先在 extractor import 报错——先完成 Task 4 安装依赖后再验）

- [ ] **Step 5: Commit**

```bash
git add src/mem_lake/cli/import_requirements.py src/mem_lake/cli/__init__.py
git commit -m "feat(cli): add import-requirements CLI entrypoint"
```

---

## Task 4: `pyproject.toml` — 依赖与 console script

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add markitdown dependency & console script**

在 `[project].dependencies` 列表末尾追加：`   markitdown>=0.1.0` （HTML 解析用内置 HtmlConverter，无需 extras）

在 `[project].scripts` 追加：
```toml
memlake-import-requirements = "mem_lake.cli.import_requirements:run"
```

具体 diff：
```toml
dependencies = [
    ...
    structlog>=24.0.0,
    markitdown>=0.1.0,
]

[project.scripts]
memlake-bootstrap-admin = "mem_lake.cli:run"
memlake-import-requirements = "mem_lake.cli.import_requirements:run"
```

- [ ] **Step 2: Install markitdown into the conda env & install package (editable)**

Run:
```bash
D:\.conda\envs\memlake\python.exe -m pip install "markitdown>=0.1.0"
D:\.conda\envs\memlake\python.exe -m pip install -e .
```
Expected: markitdown installed；`memlake-import-requirements` 可执行脚本注册。

- [ ] **Step 3: Re-run all new tests**

Run: `D:\.conda\envs\memlake\python.exe -m pytest tests/unit/test_cli_extractor.py tests/unit/test_cli_ingest.py -v`
Expected: 全部 PASS（此时 markitdown 已安装，extractor 真实转换生效；若 HTML 转换依赖缺 BeautifulSoup 会报 MissingDependencyException，则加装 `beautifulsoup4`，并在本 Task Step 2 一起装）

- [ ] **Step 4: Verify console script resolves**

Run: `D:\.conda\envs\memlake\python.exe -c "from importlib.metadata import entry_points; [print(ep) for ep in entry_points(group='console_scripts') if 'import' in ep.name.lower() or 'requirements' in ep.name.lower()]"`
Expected: 至少列出 `memlake-import-requirements`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml
git commit -m "build: add markitdown dep and memlake-import-requirements console script"
```

---

## Task 5: `ingest_requirement` 集成测试（真实 create_node/update_node + 幂等）

**Files:**
- Add tests to: `tests/unit/test_cli_ingest.py`

- [ ] **Step 1: Write the failing test**

在 `tests/unit/test_cli_ingest.py` 追加（需 `db_session`/`graph_store`/`mock_embedding_client` fixture，来自 `tests/conftest.py`）：

```python
import uuid

import pytest
from sqlalchemy import select

from mem_lake.cli.extractor import ParsedRequirement
from mem_lake.cli.ingest import ingest_requirement, find_requirement_by_source
from mem_lake.knowledge.models import KnowledgeNode, System


@pytest.mark.asyncio
async def test_ingest_requirement_create_then_skip_then_force_update(
    db_session, graph_store, mock_embedding_client, knowledge_helpers
):
    """create → skip（幂等）→ force 更新，source_doc 不重复。"""
    # 建 system（需 code）+ 前置 SystemProject 不需要——Requirement 绑定 system_id/project_id
    system = System(name="HIS", code="HIS")
    db_session.add(system)
    await db_session.flush()

    project_id = uuid.uuid4()
    parsed = ParsedRequirement(title="HIS/auth/login.html", content="实现 JWT 登录", rel_path="HIS/auth/login.html")

    # 1) create
    r1 = await ingest_requirement(
        db_session,
        graph_store=graph_store,
        embedding_client=mock_embedding_client,
        project_id=project_id,
        system_id=system.id,
        parsed=parsed,
        priority="P3",
        module="导入",
        force=False,
        actor="cli-import",
        created_by="cli-import",
    )
    assert r1["status"] == "created"

    # 2) 幂等：再跑一次 → skipped，不新增
    r2 = await ingest_requirement(
        db_session,
        graph_store=graph_store,
        embedding_client=mock_embedding_client,
        project_id=project_id,
        system_id=system.id,
        parsed=parsed,
        priority="P3",
        module="导入",
        force=False,
        actor="cli-import",
        created_by="cli-import",
    )
    assert r2["status"] == "skipped"

    # 3) force → updated，且 properties.source_doc 保留、内容更新
    parsed_updated = ParsedRequirement(
        title="HIS/auth/login.html", content="实现 JWT 登录(改)", rel_path="HIS/auth/login.html"
    )
    r3 = await ingest_requirement(
        db_session,
        graph_store=graph_store,
        embedding_client=mock_embedding_client,
        project_id=project_id,
        system_id=system.id,
        parsed=parsed_updated,
        priority="P3",
        module="导入",
        force=True,
        actor="cli-import",
        created_by="cli-import",
    )
    assert r3["status"] == "updated"

    # 同 source_doc 仍只有一条
    rows = (
        await db_session.execute(
            select(KnowledgeNode).where(KnowledgeNode.type == "Requirement")
        )
    ).scalars().all()
    reqs = [r for r in rows if (r.properties or {}).get("source_doc") == "HIS/auth/login.html"]
    assert len(reqs) == 1
    assert reqs[0].content == "实现 JWT 登录(改)"
    assert reqs[0].properties["priority"] == "P3"


@pytest.mark.asyncio
async def test_find_requirement_by_source_filters_by_project_and_system(
    db_session, graph_store, mock_embedding_client
):
    """source_doc 匹配限定 project+system，跨 system 不算重复。"""
    sys_a = System(name="A", code="A")
    sys_b = System(name="B", code="B")
    db_session.add_all([sys_a, sys_b])
    await db_session.flush()

    proj_a = uuid.uuid4()
    proj_b = uuid.uuid4()
    parsed = ParsedRequirement(title="x.html", content="c", rel_path="x.html")

    for pid, sid in [(proj_a, sys_a.id), (proj_b, sys_b.id)]:
        await ingest_requirement(
            db_session,
            graph_store=graph_store,
            embedding_client=mock_embedding_client,
            project_id=pid, system_id=sid, parsed=parsed,
            priority="P3", module="导入", force=False,
            actor="cli", created_by="cli",
        )
        await db_session.flush()

    found_a = await find_requirement_by_source(
        db_session, project_id=proj_a, system_id=sys_a.id, source_doc="x.html"
    )
    found_b = await find_requirement_by_source(
        db_session, project_id=proj_b, system_id=sys_b.id, source_doc="x.html"
    )
    assert found_a is not None and found_b is not None
    assert found_a.id != found_b.id  # 各自独立，不互相视为重复
```

- [ ] **Step 2: Run test to verify it fails / passes**

Run: `D:\.conda\envs\memlake\python.exe -m pytest tests/unit/test_cli_ingest.py -k "ingest_requirement or find_requirement" -v`
Expected: PASS（`ingest_requirement`/`find_requirement_by_source` 已在 Task 2 实现；真实路径需 DB 容器在线与 AGE 可用——若容器不可用会连接失败，需先确认部署容器运行中）

> 若运行环境连不上 Postgres/AGE，此集成测试会 fail。该测试需要真实 DB（`db_session` fixture）。生产 CI 请保证容器在线；本地开发同样依赖容器。若确需纯单测隔离，可只用 Task 2 的注入 fake 测试 + 手动验证，并在 CI 上跑本集成。

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_cli_ingest.py
git commit -m "test(cli): add ingest_requirement idempotency integration tests"
```

---

## Task 6: 全量回归与 lint

**Files:**
- 无代码改动（验证）

- [ ] **Step 1: Run ruff lint on new files**

Run: `D:\.conda\envs\memlake\python.exe -m ruff check src/mem_lake/cli/extractor.py src/mem_lake/cli/ingest.py src/mem_lake/cli/import_requirements.py tests/unit/test_cli_extractor.py tests/unit/test_cli_ingest.py`
Expected: `All checks passed!`（若报 noqa/SIM 等按提示修）

- [ ] **Step 2: Run full unit test suite**

Run: `D:\.conda\envs\memlake\python.exe -m pytest tests/unit -q`
Expected: 既有单测全部通过（新增 CLI 测试亦在其中）。若因 DB 容器未启动个别集成用例 skip/fail，以既有基线为准。

- [ ] **Step 3: Confirm no regressions in existing knowledge/repository tests**

Run: `D:\.conda\envs\memlake\python.exe -m pytest tests/integration/test_knowledge_repository.py -q`
Expected: PASS

---

## Self-Review

**Spec coverage:**
- CLI 入口调用形态 → Task 3 (`import_requirements.py`, console script)。
- markitdown 解析 HTML→md → Task 1 (extractor) + Task 4 (markitdown 依赖)。
- title=完整相对路径(`/`) → Task 1 `_to_posix`。
- content=markdown 正文 → Task 1。
- priority/module CLI 默认值 → Task 3 (`--priority`/`--module`) + Task 2 properties 映射。
- system/project 参数 → Task 3 (`--system-code`/`--project`) + resolve_system fail-fast。
- 直接入库（create_node，带向量、不进审批）→ Task 2。
- source_doc 幂等 + `--force` update → Task 2 + Task 5 集成测试。
- dry-run → Task 3 + Task 2 `run_import(dry_run=True)`。
- 单文件失败隔离 → Task 2 `run_import` try/except。
- 测试 → Task 1/2/3/5。

**Placeholder scan:** 无 TBD/TODO；代码块齐全。

**Type consistency:** `ParsedRequirement(title, content, rel_path)` 在 extractor 定义、ingest/测试引用一致；`run_import` 参数名在 Task 2 定义、Task 3 调用一致；`ingest_requirement` 返回 `{"status","title"}` 与 `run_import` 消费一致。

---

## Execution Handoff

计划已保存至 `docs/superpowers/plans/2026-08-31-import-requirements.md`。

两个执行选项：
1. **Subagent-Driven（推荐）** — 每个 Task 派发独立 subagent，任务间人工审查，迭代快。
2. **Inline Execution** — 本会话按 executing-plans 批量执行，带检查点。

采用哪个？
