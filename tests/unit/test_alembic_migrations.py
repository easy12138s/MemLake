"""Alembic 迁移脚本链完整性护栏。

单 head 断言：防止多 head / 迁移漏登记（启动期 check_migrations_synced 依赖两侧一致）。
"""


def test_migration_script_has_single_head():
    """脚本目录单一 head（防多 head / 断链；新增迁移无需改本用例）。"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", "alembic")
    assert len(ScriptDirectory.from_config(cfg).get_heads()) == 1


def test_approval_batch_submission_index_name_matches_migration():
    """ORM 索引名必须与 0005/0007 迁移一致，避免 autogenerate 漂移。"""
    from mem_lake.approval.models import ApprovalBatch

    names = {idx.name for idx in ApprovalBatch.__table__.indexes}

    assert "idx_approval_batch_submission" in names
    assert "ix_approval_batch_submission_id" not in names
