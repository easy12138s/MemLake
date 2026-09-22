"""批量导入需求文档：解析层。

将文件夹内的需求文档解析为 ParsedRequirement 列表。
通过 RequirementAdapter 协议实现可插拔的文件解析策略：
parse 返回 list（0..N 条/文件），数据源个性化处理（标题、拆分、元数据）
全部封装在适配器内。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ParsedRequirement:
    """单个已解析需求。

    title 与 rel_path 由适配器决定：
    - html 适配器：title=完整相对路径（`/` 连接）
    - notes 适配器：title=文件名 stem；拆分条目 title=`{stem} #{i}`，
      rel_path=`{rel_path}#{i}`（`#N` 为条目级幂等键，供 ingest source_doc 去重）
    content 为正文（md 原文或 html 转换后的 markdown）。
    """

    title: str
    content: str
    rel_path: str


@runtime_checkable
class RequirementAdapter(Protocol):
    """运行时可检查的适配器协议：决定文件是否接受、如何解析。

    parse 返回 0..N 条需求：空列表表示该文件不产生任何需求（跳过）。
    """

    def accepts(self, file: Path) -> bool: ...

    def parse(self, file: Path, rel_path: str) -> list[ParsedRequirement]: ...


def _to_posix(path: Path) -> str:
    """转为 `/` 连接的相对路径（跨平台），如 'HIS/auth/login.html'。"""
    return path.as_posix()


def extract_directory(
    root: str | Path, adapter: RequirementAdapter | None = None
) -> list[ParsedRequirement]:
    """递归扫描 root 下所有文件，用 adapter 决定是否接受并解析为 ParsedRequirement。

    adapter 为 None 时使用默认 MarkdownHtmlAdapter。
    parse 返回空列表或解析失败的文件跳过并打印告警（不抛）。
    root 不存在抛 NotADirectoryError。
    """
    from mem_lake.cli.adapters import MarkdownHtmlAdapter

    if adapter is None:
        adapter = MarkdownHtmlAdapter()

    base = Path(root)
    if not base.is_dir():
        raise NotADirectoryError(f"目录不存在: {base}")

    parsed: list[ParsedRequirement] = []
    for file in sorted(base.rglob("*")):
        if not file.is_file() or not adapter.accepts(file):
            continue
        rel = _to_posix(file.relative_to(base))
        try:
            items = adapter.parse(file, rel)
        except Exception as exc:
            print(f"[跳过] {rel}: 解析失败 ({type(exc).__name__}: {exc})")
            continue
        if not items:
            print(f"[跳过] {rel}: 解析结果为空")
            continue
        parsed.extend(items)
    return parsed
