"""批量导入需求文档：适配器注册表与实现。"""
from __future__ import annotations

from pathlib import Path

from markitdown import MarkItDown

from mem_lake.cli.extractor import ParsedRequirement

_HTML_SUFFIXES = {".html", ".htm"}
_INDEX_NAMES = {"index.html", "index.htm"}


class MarkdownHtmlAdapter:
    """默认适配器：接受 .html/.htm（排除 index 导航壳），markitdown 全文转 markdown。"""

    def __init__(self) -> None:
        self._md = MarkItDown()

    def accepts(self, file: Path) -> bool:
        if file.suffix.lower() not in _HTML_SUFFIXES:
            return False
        return file.name.lower() not in _INDEX_NAMES

    def parse(self, file: Path, rel_path: str) -> ParsedRequirement | None:
        result = self._md.convert(file)
        content = (result.text_content or "").strip()
        if not content:
            return None
        return ParsedRequirement(title=rel_path, content=content, rel_path=rel_path)
