"""add access_key name/owner columns

C1 / ISSUE-18：审计 actor 只有裸 UUID，无法辨认操作者。新增两列：

- name：可读名称/用途标签（如 示例系统-张三-dev），用于 list_access_keys 展示与
  query_audit_log 的 actor_name 回显；
- owner：使用人/归属人（可选）。

迁移策略（共享库在线）：
- 两列均 nullable 且**无 server_default** → PG 11+ ADD COLUMN 不重写表，在线执行；
- 存量行保持 NULL（不回填，非破坏性）；新 Key 可选命名；
- 审计 actor 仍保留 Key UUID（兼容既有消费方），仅新增 actor_name 回显。

Revision ID: 0004_access_key_name_owner
Revises: 0003_drop_content_vector
Create Date: 2026-11-20 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0004_access_key_name_owner'
down_revision: Union[str, Sequence[str], None] = '0003_drop_content_vector'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """新增 name/owner 两列（nullable 无默认值，在线加列不重写表）。"""
    op.add_column(
        'access_key',
        sa.Column(
            'name',
            sa.String(length=64),
            nullable=True,
            comment='可读名称/用途标签（如 示例系统-张三-dev）；NULL=未登记',
        ),
    )
    op.add_column(
        'access_key',
        sa.Column(
            'owner',
            sa.String(length=128),
            nullable=True,
            comment='使用人/归属人（可选，便于辨认；审计仍保留 Key UUID）',
        ),
    )


def downgrade() -> None:
    """回退：删除 owner/name 两列（存量命名信息随之丢失）。"""
    op.drop_column('access_key', 'owner')
    op.drop_column('access_key', 'name')
