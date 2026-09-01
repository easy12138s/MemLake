"""批量导入需求文档：适配器注册表与实现。"""
from __future__ import annotations

import re
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


class AxureCleanedAdapter:
    """对 Axure RP HTML 导出：markitdown 转 markdown 后做轻度后处理清洗。

    实测：markitdown 对 Axure 叶子页输出已干净（无 uNNNN/注释/svg 泄漏）；
    index 导航壳由 accepts 排除。此处仅折叠多余空行、去首尾空白。
    """

    # 连续 2 个以上空行折叠为单个空行
    _BLANK_RE = re.compile(r"\n[ \t]*(?:\n[ \t]*)+")

    def __init__(self) -> None:
        self._md = MarkItDown()

    def accepts(self, file: Path) -> bool:
        if file.suffix.lower() not in _HTML_SUFFIXES:
            return False
        return file.name.lower() not in _INDEX_NAMES

    def parse(self, file: Path, rel_path: str) -> ParsedRequirement | None:
        result = self._md.convert(file)
        content = self._clean(result.text_content or "")
        if not content.strip():
            return None
        return ParsedRequirement(title=rel_path, content=content.strip(), rel_path=rel_path)

    def _clean(self, text: str) -> str:
        return self._BLANK_RE.sub("\n", text).strip()


ADAPTERS: dict[str, type[MarkdownHtmlAdapter] | type[AxureCleanedAdapter]] = {
    "markdown": MarkdownHtmlAdapter,
    "axure": AxureCleanedAdapter,
}


def get_adapter(name: str) -> MarkdownHtmlAdapter | AxureCleanedAdapter:
    """按名称取适配器实例；未知名抛 ValueError。"""
    try:
        cls = ADAPTERS[name]
    except KeyError:
        raise ValueError(
            f"未知 adapter: {name!r}（可选: {sorted(ADAPTERS)}）"
        ) from None
    return cls()
