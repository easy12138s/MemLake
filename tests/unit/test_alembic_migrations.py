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
