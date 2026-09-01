"""adapters 单测：MarkdownHtmlAdapter / AxureCleanedAdapter 的 accepts 与 parse + 注册表。"""

from pathlib import Path

import pytest

from mem_lake.cli.adapters import (
    AxureCleanedAdapter,
    MarkdownHtmlAdapter,
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
    p = MarkdownHtmlAdapter().parse(f, "login.html")
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
    p = AxureCleanedAdapter().parse(f, "bg.html")
    assert p is not None
    assert "GSP全称是《药品经营质量管理规范》" in p.content


def test_axure_blank_lines_collapsed(tmp_path: Path) -> None:
    """_clean 把 2+ 空行折叠为单个空行。"""
    a = AxureCleanedAdapter()
    assert a._clean("a\n\n\n\nb") == "a\nb"
    assert a._clean("  文本  \n\n\n  ").strip() == "文本"


def test_axure_parse_empty_returns_none(tmp_path: Path) -> None:
    f = _write(tmp_path / "empty.html", "<html><body><!-- nothing --></body></html>")
    p = AxureCleanedAdapter().parse(f, "empty.html")
    assert p is None


def test_get_adapter_registry(tmp_path: Path) -> None:
    assert isinstance(get_adapter("markdown"), MarkdownHtmlAdapter)
    assert isinstance(get_adapter("axure"), AxureCleanedAdapter)
    with pytest.raises(ValueError):
        get_adapter("nonexistent")
