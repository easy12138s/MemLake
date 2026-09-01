"""extractor 单测：解析文件夹 HTML 需求文档。

验证 markitdown 把 HTML → markdown、title=完整相对路径(`/` 连接)、递归扫描、非 html 忽略。
"""

from pathlib import Path

import pytest

from mem_lake.cli.adapters import AxureCleanedAdapter, MarkdownHtmlAdapter, get_adapter
from mem_lake.cli.extractor import RequirementAdapter, extract_directory


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
    (tmp_path / "HIS" / "billing").mkdir()
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
    """content 为 markdown 正文（含标题与列表）。"""
    parsed = {p.title: p for p in extract_directory(req_tree)}
    login = parsed["HIS/auth/login.html"]
    assert "用户登录" in login.content
    assert "JWT" in login.content
    # markdown 列表项（markitdown 用 `* ` 或 `- ` 渲染 <li>）
    assert any(line.lstrip().startswith(("-", "*")) for line in login.content.splitlines())


def test_ignores_non_html_files(req_tree: Path) -> None:
    """非 .html/.htm 文件被忽略。"""
    parsed = extract_directory(req_tree)
    assert all(p.title.endswith(".html") for p in parsed)
    assert not any("readme.txt" in p.title for p in parsed)


def test_empty_directory_returns_empty(tmp_path: Path) -> None:
    assert extract_directory(tmp_path) == []


def test_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        extract_directory(tmp_path / "nope")


def test_requirement_adapter_is_protocol():
    """RequirementAdapter 是运行时协议，可被实例 class 满足。"""
    assert isinstance(getattr(RequirementAdapter, "_is_protocol", True), bool)
    assert isinstance(MarkdownHtmlAdapter(), RequirementAdapter)


def test_markdown_adapter_accepts_html_not_index(req_tree: Path) -> None:
    """markdown 适配器接受 .html/.htm；index.html 被排除。"""
    a = MarkdownHtmlAdapter()
    assert a.accepts(req_tree / "HIS" / "auth" / "login.html")
    assert not a.accepts(req_tree / "HIS" / "index.html")
    assert not a.accepts(req_tree / "HIS" / "readme.txt")


def test_extract_directory_uses_adapter(req_tree: Path) -> None:
    """extract_directory 默认用 markdown 适配器；title 含 posix 相对路径。"""
    parsed = extract_directory(req_tree)
    assert {p.title for p in parsed} == {"HIS/auth/login.html", "HIS/billing/invoice.html"}


def test_extract_directory_excludes_top_and_nested_index(req_tree: Path) -> None:
    """显式注入适配器后 index.html 仍被排除（自上而下层级都应排除）。"""
    (req_tree / "HIS" / "index.html").write_text("<html><body>nav</body></html>", encoding="utf-8")
    parsed = extract_directory(req_tree, adapter=MarkdownHtmlAdapter())
    assert not any(p.title.endswith("index.html") for p in parsed)


def test_get_adapter_registry() -> None:
    """注册表可通过名称取到对应适配器；未知名抛 ValueError。"""
    assert isinstance(get_adapter("markdown"), MarkdownHtmlAdapter)
    assert isinstance(get_adapter("axure"), AxureCleanedAdapter)
    with pytest.raises(ValueError):
        get_adapter("nope")
