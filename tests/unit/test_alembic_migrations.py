"""Alembic 迁移脚本链完整性护栏。

单 head 断言：防止多 head / 迁移漏登记（启动期 check_migrations_synced 依赖两侧一致）。
"""


def test_single_head_is_0004_access_key_name_owner():
    """脚本目录单一 head，且为 access_key name/owner 迁移（C1/ISSUE-18）。"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", "alembic")
    script = ScriptDirectory.from_config(cfg)
    assert script.get_heads() == ["0004_access_key_name_owner"]
