"""冲突检测查询文本构造一致性守护测试（审计 §2.1）。

背景：落库向量用 build_embed_text（title+content+属性段）构造，而冲突检测
查询文本曾是 f"{title}\n{content}"，两者不一致会系统性拉低 query-doc 相似度。
本测试守护三处查询文本构造与 build_embed_text 保持一致：
- approval/conflict.py detect_conflicts 内部 embed
- approval/service.py review_approve / auto_process_batch 批量预计算
- gateway/tools/search_tools.py check_requirement_conflicts
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

from mem_lake.approval.conflict import detect_conflicts
from mem_lake.knowledge.embed import build_embed_text


def _make_session():
    """可 await 的 session mock（execute 返回空结果，跳过 L0 硬键查询）。"""
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)
    return session


def _make_vector_searcher(results):
    searcher = MagicMock()
    searcher.search = AsyncMock(return_value=results)
    searcher.search_by_vector = AsyncMock(return_value=results)
    return searcher


def _make_embedding_client():
    client = MagicMock()
    client.embed_one = AsyncMock(return_value=[0.1] * 1024)
    return client


class TestConflictQueryTextConsistency:
    """detect_conflicts 的查询文本必须与落库向量构造（build_embed_text）一致。"""

    async def test_detect_conflicts_embeds_build_embed_text(self):
        """detect_conflicts 无预计算向量时，embed 输入 = build_embed_text 输出。"""
        project_id = uuid.uuid4()
        vector_searcher = _make_vector_searcher([])

        title = "登录需求"
        content = "支持账号密码登录"
        properties = {"priority": "P0", "module": "auth"}

        await detect_conflicts(
            _make_session(),
            vector_searcher=vector_searcher,
            project_id=project_id,
            node_type="Requirement",
            title=title,
            content=content,
            properties=properties,
            tags=["auth"],
        )

        expected = build_embed_text("Requirement", title, content, properties)
        vector_searcher.search.assert_awaited_once()
        actual = vector_searcher.search.call_args.args[-1]
        assert actual == expected
        # 属性段确实参与（含 priority/module 键值）
        assert "priority" in expected
        assert "module" in expected

    async def test_build_embed_text_contains_property_segment(self):
        """build_embed_text 结构守护：title + content + 属性段。"""
        text = build_embed_text(
            "Pitfall",
            "踩坑标题",
            "踩坑正文",
            {"symptom": "报错X", "root_cause": "根因Y", "solution": "解法Z"},
        )
        assert "踩坑标题" in text
        assert "踩坑正文" in text
        assert "symptom" in text
        assert "根因Y" in text
