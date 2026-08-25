"""build_embed_text / build_embed_facets 单元测试（P2 向量富集 + 32k 多向量）。"""

import pytest

from mem_lake.knowledge.embed import (
    EMBED_PROPERTY_FIELDS,
    FACET_CONTENT,
    _render_value,
    build_embed_facets,
    build_embed_text,
)


def test_includes_key_properties_for_known_type():
    text = build_embed_text(
        "Pitfall",
        "async session 泄漏",
        "连接池耗尽",
        {"symptom": "PoolExhausted", "root_cause": "未 close", "solution": "用 async with", "severity": "P1"},
    )
    assert "async session 泄漏" in text
    assert "连接池耗尽" in text
    # 关键属性被纳入
    assert "root_cause: 未 close" in text
    assert "symptom: PoolExhausted" in text
    assert "severity: P1" in text


def test_skips_empty_properties():
    text = build_embed_text("CodeSnippet", "登录服务", "实现", {})
    assert text == "登录服务\n实现"


def test_unknown_type_only_title_content():
    text = build_embed_text(
        "UnknownType", "标题", "正文", {"foo": "bar"}
    )
    assert text == "标题\n正文"


def test_property_order_follows_schema():
    # CodeSnippet 顺序应为 name/type/responsibility/file_path/language
    text = build_embed_text(
        "CodeSnippet",
        "t",
        "c",
        {"language": "py", "name": "Svc", "responsibility": "认证"},
    )
    idx_name = text.index("name: Svc")
    idx_resp = text.index("responsibility: 认证")
    idx_lang = text.index("language: py")
    assert idx_name < idx_resp < idx_lang


def test_render_value_list_and_dict():
    assert _render_value(["a", "b"]) == "a b"
    assert _render_value({"k": "v"}) == "k=v"


def test_long_value_truncated():
    # 32k 适配（A）：属性截断上限由 512 放宽到 4096
    long_val = "x" * 9999
    rendered = _render_value(long_val)
    assert len(rendered) <= 4096 + 3
    assert rendered.endswith("...")


def test_build_embed_facets_content_and_properties():
    # 32k 适配（D）：多向量 facet 构造——content facet + 各非空关键属性独立成 facet
    facets = build_embed_facets(
        "Pitfall",
        "async session 泄漏",
        "连接池耗尽",
        {"symptom": "PoolExhausted", "root_cause": "未 close", "solution": "用 async with"},
    )
    assert facets[FACET_CONTENT] == "async session 泄漏\n连接池耗尽"
    assert facets["symptom"] == "symptom: PoolExhausted"
    assert facets["root_cause"] == "root_cause: 未 close"
    assert facets["solution"] == "solution: 用 async with"
    # 未提供的关键属性（severity）不出现
    assert "severity" not in facets


def test_build_embed_facets_empty_returns_empty():
    facets = build_embed_facets("CodeSnippet", "", "", {})
    assert facets == {}


def test_build_embed_facets_content_only_when_no_props():
    facets = build_embed_facets("UnknownType", "标题", "正文", {"foo": "bar"})
    assert list(facets.keys()) == [FACET_CONTENT]
    assert facets[FACET_CONTENT] == "标题\n正文"


def test_all_types_have_field_config():
    # 防止 NODE_TYPES 与 EMBED_PROPERTY_FIELDS 脱节
    from mem_lake.knowledge.schema import NODE_TYPES

    for t in NODE_TYPES:
        assert t in EMBED_PROPERTY_FIELDS, f"{t} 缺少嵌入属性配置"
