"""import_requirements CLI 单测：--batch-size、dry-run、main 调度。

不依赖 DB / embedding / graph_store；用 monkeypatch 隔离。
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mem_lake.cli.import_requirements import _parse_args, main


class _FakeSystem:
    def __init__(self, name: str = "HIS", code: str | None = "HIS"):
        self.id = uuid.uuid4()
        self.name = name
        self.code = code


# ---------------------------------------------------------------------------
# _parse_args
# ---------------------------------------------------------------------------

def test_parse_args_defaults():
    args = _parse_args(["some/folder"])
    assert args.folder == "some/folder"
    assert args.adapter == "markdown"
    assert args.priority == "P3"
    assert args.module == "导入"
    assert args.dry_run is False
    assert args.batch_size == 50


def test_parse_args_batch_size():
    args = _parse_args(["f", "--batch-size", "10"])
    assert args.batch_size == 10


def test_parse_args_dry_run():
    args = _parse_args(["f", "--dry-run"])
    assert args.dry_run is True


def test_parse_args_system_name():
    args = _parse_args(["f", "--system-name", "中方诊药云系统"])
    assert args.system_name == "中方诊药云系统"
    assert args.system_code is None


# ---------------------------------------------------------------------------
# dry-run: no DB writes, prints pending
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_main_dry_run_no_db_writes(tmp_path):
    """dry_run 不创建 embedding_client / graph_store，不调用 run_import_batch。"""
    (tmp_path / "a.html").write_text("<h1>A</h1>", encoding="utf-8")

    fake_system = _FakeSystem()
    mock_session = AsyncMock()

    with (
        patch("mem_lake.cli.import_requirements.AsyncSessionLocal") as mock_local,
        patch("mem_lake.cli.import_requirements.resolve_system", new_callable=AsyncMock) as mock_resolve,
        patch("mem_lake.cli.import_requirements.extract_directory") as mock_extract,
        patch("mem_lake.cli.import_requirements.run_import_batch") as mock_batch,
        patch("mem_lake.cli.import_requirements.get_embedding_client") as mock_emb,
        patch("mem_lake.cli.import_requirements.get_graph_store") as mock_graph,
    ):
        mock_local.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_local.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_resolve.return_value = fake_system

        from mem_lake.cli.extractor import ParsedRequirement

        parsed = ParsedRequirement(title="a.html", content="x", rel_path="a.html")
        mock_extract.return_value = [parsed]

        rc = await main([str(tmp_path), "--system-name", "HIS", "--dry-run"])

    assert rc == 0
    mock_batch.assert_not_called()
    mock_emb.assert_not_called()
    mock_graph.assert_not_called()


@pytest.mark.asyncio
async def test_main_invalid_project_uuid_returns_2():
    rc = await main(["some/folder", "--project", "not-a-uuid"])
    assert rc == 2


# ---------------------------------------------------------------------------
# real import: calls run_import_batch with correct args
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_main_real_import_calls_batch(tmp_path):
    """非 dry-run 时调用 run_import_batch 且传入所有必要参数。"""
    fake_system = _FakeSystem()
    mock_session = AsyncMock()
    mock_summary = MagicMock()
    mock_summary.failed = []
    mock_summary.created = ["a.html"]
    mock_summary.skipped = []

    with (
        patch("mem_lake.cli.import_requirements.AsyncSessionLocal") as mock_local,
        patch("mem_lake.cli.import_requirements.resolve_system", new_callable=AsyncMock) as mock_resolve,
        patch("mem_lake.cli.import_requirements.run_import_batch", new_callable=AsyncMock) as mock_batch,
        patch("mem_lake.cli.import_requirements.get_embedding_client") as mock_emb,
        patch("mem_lake.cli.import_requirements.get_graph_store") as mock_graph,
    ):
        mock_local.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_local.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_resolve.return_value = fake_system
        mock_batch.return_value = mock_summary

        rc = await main([
            str(tmp_path), "--system-name", "HIS", "--batch-size", "25",
            "--priority", "P1", "--module", "云HIS",
        ])

    assert rc == 0
    mock_batch.assert_awaited_once()
    call_kwargs = mock_batch.call_args[1]
    assert call_kwargs["batch_size"] == 25
    assert call_kwargs["priority"] == "P1"
    assert call_kwargs["module"] == "云HIS"
    assert call_kwargs["system"] is fake_system
    mock_emb.assert_called_once()
    mock_graph.assert_called_once()


@pytest.mark.asyncio
async def test_main_failed_returns_1(tmp_path):
    """summary.failed 非空时返回退出码 1。"""
    fake_system = _FakeSystem()
    mock_session = AsyncMock()
    mock_summary = MagicMock()
    mock_summary.failed = ["bad.html"]
    mock_summary.created = []
    mock_summary.skipped = []

    with (
        patch("mem_lake.cli.import_requirements.AsyncSessionLocal") as mock_local,
        patch("mem_lake.cli.import_requirements.resolve_system", new_callable=AsyncMock) as mock_resolve,
        patch("mem_lake.cli.import_requirements.run_import_batch", new_callable=AsyncMock) as mock_batch,
        patch("mem_lake.cli.import_requirements.get_embedding_client"),
        patch("mem_lake.cli.import_requirements.get_graph_store"),
    ):
        mock_local.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_local.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_resolve.return_value = fake_system
        mock_batch.return_value = mock_summary

        rc = await main([str(tmp_path), "--system-name", "HIS"])

    assert rc == 1
