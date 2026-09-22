"""adapters 单测：MarkdownHtmlAdapter / AxureCleanedAdapter / MarkdownNotesAdapter 的 accepts 与 parse + 注册表。

parse 协议：返回 list[ParsedRequirement]（0..N 条/文件；空列表=跳过该文件）。
"""

from pathlib import Path

import pytest

from mem_lake.cli.adapters import (
    AxureCleanedAdapter,
    MarkdownHtmlAdapter,
    MarkdownNotesAdapter,
    get_adapter,
)
from mem_lake.cli.extractor import ParsedRequirement


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_markdown_accepts_html_suffixes(tmp_path: Path) -> None:
    a = MarkdownHtmlAdapter()
    assert a.accepts(_write(tmp_path / "a.html", "<p>x</p>"))
    assert a.accepts(_write(tmp_path / "a.htm", "<p>x</p>"))
    assert not a.accepts(_write(tmp_path / "a.txt", "x"))
    assert not a.accepts(tmp_path / "noext")
    assert not a.accepts(_write(tmp_path / "a.png", ""))


def test_markdown_rejects_index_html(tmp_path: Path) -> None:
    a = MarkdownHtmlAdapter()
    assert not a.accepts(_write(tmp_path / "index.html", "<p>nav</p>"))
    assert not a.accepts(_write(tmp_path / "docs" / "index.html", "<p>nav</p>"))


def test_markdown_parse_returns_parsed(tmp_path: Path) -> None:
    f = _write(tmp_path / "login.html", "<html><body><h1>用户登录</h1><p>JWT 登录</p></body></html>")
    items = MarkdownHtmlAdapter().parse(f, "login.html")
    assert len(items) == 1
    p = items[0]
    assert isinstance(p, ParsedRequirement)
    assert p.title == "login.html"
    assert p.rel_path == "login.html"
    assert "JWT" in p.content


def test_axure_accepts_excludes_index_html(tmp_path: Path) -> None:
    a = AxureCleanedAdapter()
    assert a.accepts(_write(tmp_path / "leaf.html", "<body>c</body>"))
    assert not a.accepts(_write(tmp_path / "index.html", "<p>nav</p>"))
    assert not a.accepts(_write(tmp_path / "sub" / "index.html", "<p>nav</p>"))
    assert not a.accepts(_write(tmp_path / "a.txt", "x"))


def test_axure_cleaning_preserves_readable_text(tmp_path: Path) -> None:
    """清洗后保留正文可读文本。"""
    html = (
        "<html><body>"
        '<div class="ax_default heading_3"><div class="text"><p><span>说明</span></p></div></div>'
        '<div class="ax_default paragraph1"><div class="text"><p>'
        "<span>GSP全称是《药品经营质量管理规范》</span>"
        "</p></div></div>"
        "</body></html>"
    )
    f = _write(tmp_path / "bg.html", html)
    items = AxureCleanedAdapter().parse(f, "bg.html")
    assert len(items) == 1
    assert "GSP全称是《药品经营质量管理规范》" in items[0].content


def test_axure_blank_lines_collapsed(tmp_path: Path) -> None:
    """_clean 把 2+ 空行折叠为单个空行。"""
    a = AxureCleanedAdapter()
    assert a._clean("a\n\n\n\nb") == "a\nb"
    assert a._clean("  文本  \n\n\n  ").strip() == "文本"


def test_axure_parse_empty_returns_empty_list(tmp_path: Path) -> None:
    f = _write(tmp_path / "empty.html", "<html><body><!-- nothing --></body></html>")
    assert AxureCleanedAdapter().parse(f, "empty.html") == []


def test_get_adapter_registry(tmp_path: Path) -> None:
    assert isinstance(get_adapter("markdown"), MarkdownHtmlAdapter)
    assert isinstance(get_adapter("axure"), AxureCleanedAdapter)
    assert isinstance(get_adapter("notes"), MarkdownNotesAdapter)
    with pytest.raises(ValueError):
        get_adapter("nonexistent")


# ============================================================================
# MarkdownNotesAdapter：.md 需求笔记（文件名=标题；>500 字按两个空行拆分）
# ============================================================================


def test_notes_accepts_md_excluding_index(tmp_path: Path) -> None:
    a = MarkdownNotesAdapter()
    assert a.accepts(_write(tmp_path / "login.md", "x"))
    assert not a.accepts(_write(tmp_path / "index.md", "x"))
    assert not a.accepts(_write(tmp_path / "sub" / "index.md", "x"))
    assert not a.accepts(_write(tmp_path / "a.html", "x"))
    assert not a.accepts(_write(tmp_path / "a.txt", "x"))


def test_notes_short_content_single_item_title_is_stem(tmp_path: Path) -> None:
    f = _write(tmp_path / "登录页.md", "用户登录需求：支持手机号验证码登录。")
    items = MarkdownNotesAdapter().parse(f, "登录页.md")
    assert len(items) == 1
    assert items[0].title == "登录页"
    assert items[0].rel_path == "登录页.md"
    assert "验证码" in items[0].content


def test_notes_split_threshold_boundary(tmp_path: Path) -> None:
    """恰 500 字不拆；501 字拆分。"""
    f_ok = _write(tmp_path / "ok.md", "a" * 248 + "\n\n\n" + "b" * 249)  # len=500
    assert len(MarkdownNotesAdapter().parse(f_ok, "ok.md")) == 1

    f_501 = _write(tmp_path / "s.md", "a" * 248 + "\n\n\n" + "b" * 250)  # len=501
    assert len(MarkdownNotesAdapter().parse(f_501, "s.md")) == 2


def test_notes_split_items_have_numbered_titles_and_keys(tmp_path: Path) -> None:
    seg1 = "需求点一：" + "甲" * 300
    seg2 = "需求点二：" + "乙" * 200
    seg3 = "需求点三"
    f = _write(tmp_path / "门诊处方.md", f"{seg1}\n\n\n{seg2}\n\n\n{seg3}")
    items = MarkdownNotesAdapter().parse(f, "门诊处方.md")
    assert len(items) == 3
    assert [p.title for p in items] == ["门诊处方 #1", "门诊处方 #2", "门诊处方 #3"]
    assert [p.rel_path for p in items] == ["门诊处方.md#1", "门诊处方.md#2", "门诊处方.md#3"]
    assert items[0].content == seg1
    assert items[2].content == seg3


def test_notes_single_blank_line_not_split(tmp_path: Path) -> None:
    """单空行（markdown 段落分隔）不触发拆分。"""
    body = "甲" * 300 + "\n\n" + "乙" * 300
    f = _write(tmp_path / "one.md", body)
    items = MarkdownNotesAdapter().parse(f, "one.md")
    assert len(items) == 1
    assert "乙" in items[0].content


def test_notes_three_or_more_blank_lines_split(tmp_path: Path) -> None:
    body = "甲" * 300 + "\n\n\n\n" + "乙" * 300
    f = _write(tmp_path / "three.md", body)
    assert len(MarkdownNotesAdapter().parse(f, "three.md")) == 2


def test_notes_crlf_double_blank_splits(tmp_path: Path) -> None:
    """Windows CRLF 换行的双空行同样可拆分。"""
    body = "甲" * 300 + "\r\n\r\n\r\n" + "乙" * 300
    f = _write(tmp_path / "crlf.md", body)
    assert len(MarkdownNotesAdapter().parse(f, "crlf.md")) == 2


def test_notes_split_segments_stripped(tmp_path: Path) -> None:
    """分割点含空白行时空白不进入段落正文。"""
    body = "甲" * 300 + "\n\n\n  \n" + "乙" * 300
    f = _write(tmp_path / "ws.md", body)
    items = MarkdownNotesAdapter().parse(f, "ws.md")
    assert len(items) == 2
    assert items[0].content == "甲" * 300
    assert items[1].content == "乙" * 300


def test_notes_long_without_separator_fallback_single(tmp_path: Path) -> None:
    """>500 字但无双空行分隔符：回落单条（rel_path 不带 #N）。"""
    f = _write(tmp_path / "plain.md", "甲" * 600)
    items = MarkdownNotesAdapter().parse(f, "plain.md")
    assert len(items) == 1
    assert items[0].title == "plain"
    assert items[0].rel_path == "plain.md"


def test_notes_empty_file_returns_empty_list(tmp_path: Path) -> None:
    f = _write(tmp_path / "empty.md", "")
    assert MarkdownNotesAdapter().parse(f, "empty.md") == []
