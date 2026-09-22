"""批量导入需求文档：适配器注册表与实现。

parse 协议：返回 list[ParsedRequirement]（0..N 条/文件，空列表=跳过）。
数据源个性化处理（标题、拆分、清洗）全部封装在适配器内，导入基础流程不感知。
"""
from __future__ import annotations

import re
from pathlib import Path

from markitdown import MarkItDown

from mem_lake.cli.extractor import ParsedRequirement, RequirementAdapter

_HTML_SUFFIXES = {".html", ".htm"}
_INDEX_NAMES = {"index.html", "index.htm", "index.md"}


class MarkdownHtmlAdapter:
    """默认适配器：接受 .html/.htm（排除 index 导航壳），markitdown 全文转 markdown。"""

    def __init__(self) -> None:
        self._md = MarkItDown()

    def accepts(self, file: Path) -> bool:
        if file.suffix.lower() not in _HTML_SUFFIXES:
            return False
        return file.name.lower() not in _INDEX_NAMES

    def parse(self, file: Path, rel_path: str) -> list[ParsedRequirement]:
        result = self._md.convert(file)
        content = (result.text_content or "").strip()
        if not content:
            return []
        return [ParsedRequirement(title=rel_path, content=content, rel_path=rel_path)]


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

    def parse(self, file: Path, rel_path: str) -> list[ParsedRequirement]:
        result = self._md.convert(file)
        content = self._clean(result.text_content or "")
        if not content.strip():
            return []
        return [ParsedRequirement(title=rel_path, content=content.strip(), rel_path=rel_path)]

    def _clean(self, text: str) -> str:
        return self._BLANK_RE.sub("\n", text).strip()


# 拆分阈值（字符，len(content)，中文字符按 1 计）。硬编码不配置：
# 阈值随笔记约定维护，暂不引入配置面。
_NOTES_SPLIT_THRESHOLD = 500


class MarkdownNotesAdapter:
    """.md 需求笔记适配器：文件名（stem）=需求标题；>500 字按连续 2+ 空行拆分。

    数据契约：同一文件内不同需求点之间用两个空行分隔（markdown 单空行段落
    不受影响）。拆分条目：
    - title="{stem} #{i}"（i 从 1，文档顺序）
    - rel_path="{rel_path}#{i}"（条目级幂等键，供 ingest source_doc 去重）
    >500 字但拆不出多段的文件回落单条（rel_path 不带 #N，与原口径一致）。
    """

    # 连续 2+ 空行（含纯空白行，兼容 CRLF）作为需求点分隔
    _SPLIT_RE = re.compile(r"\r?\n[ \t]*\r?\n[ \t]*\r?\n")

    def accepts(self, file: Path) -> bool:
        if file.suffix.lower() != ".md":
            return False
        return file.name.lower() not in _INDEX_NAMES

    def parse(self, file: Path, rel_path: str) -> list[ParsedRequirement]:
        # read_text 默认 universal newlines：CRLF 自动归一为 \n
        content = file.read_text(encoding="utf-8").strip()
        if not content:
            return []
        stem = file.stem
        if len(content) <= _NOTES_SPLIT_THRESHOLD:
            return [ParsedRequirement(title=stem, content=content, rel_path=rel_path)]
        segments = [s.strip() for s in self._SPLIT_RE.split(content) if s.strip()]
        if len(segments) <= 1:
            return [ParsedRequirement(title=stem, content=content, rel_path=rel_path)]
        return [
            ParsedRequirement(
                title=f"{stem} #{i}",
                content=seg,
                rel_path=f"{rel_path}#{i}",
            )
            for i, seg in enumerate(segments, 1)
        ]


ADAPTERS: dict[str, type[RequirementAdapter]] = {
    "markdown": MarkdownHtmlAdapter,
    "axure": AxureCleanedAdapter,
    "notes": MarkdownNotesAdapter,
}


def get_adapter(name: str) -> RequirementAdapter:
    """按名称取适配器实例；未知名抛 ValueError。"""
    try:
        cls = ADAPTERS[name]
    except KeyError:
        raise ValueError(
            f"未知 adapter: {name!r}（可选: {sorted(ADAPTERS)}）"
        ) from None
    return cls()
