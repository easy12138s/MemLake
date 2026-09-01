"""批量导入需求文档：解析层。

将文件夹内的 HTML 需求文档（每文件一个需求）解析为 ParsedRequirement。
通过 RequirementAdapter 协议实现可插拔的文件解析策略。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ParsedRequirement:
    """单个已解析需求：title 用完整相对路径（`/` 连接），content 为 markdown 正文。"""

    title: str
    content: str
    rel_path: str


@runtime_checkable
class RequirementAdapter(Protocol):
    """运行时可检查的适配器协议：决定文件是否接受、如何解析。"""

    def accepts(self, file: Path) -> bool: ...

    def parse(self, file: Path, rel_path: str) -> ParsedRequirement | None: ...


def _to_posix(path: Path) -> str:
    """转为 `/` 连接的相对路径（跨平台），如 'HIS/auth/login.html'。"""
    return path.as_posix()


def extract_directory(
    root: str | Path, adapter: RequirementAdapter | None = None
) -> list[ParsedRequirement]:
    """递归扫描 root 下所有文件，用 adapter 决定是否接受并解析为 ParsedRequirement。

    adapter 为 None 时使用默认 MarkdownHtmlAdapter。
    忽略不被 adapter 接受的文件；解析失败或结果为空的文件跳过并打印告警（不抛）。
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
            result = adapter.parse(file, rel)
        except Exception as exc:
            print(f"[跳过] {rel}: 解析失败 ({type(exc).__name__}: {exc})")
            continue
        if result is None or not result.content.strip():
            print(f"[跳过] {rel}: 解析结果为空")
            continue
        parsed.append(result)
    return parsed
