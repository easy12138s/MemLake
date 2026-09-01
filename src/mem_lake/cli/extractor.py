"""批量导入需求文档：解析层。

将文件夹内的 HTML 需求文档（每文件一个需求）解析为 ParsedRequirement。
解析用 markitdown（开源，HTML→Markdown），与入库层解耦——未来支持 docx/xlsx 等
只需在此层新增 converter 后端，不改动 ingest。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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

    忽略非 .html/.htm 文件；解析失败或结果为空的文件跳过并打印告警（不抛）。
    root 不存在抛 NotADirectoryError。
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
