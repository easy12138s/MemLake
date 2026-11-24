"""add approval_batch submission_id

批量发布分组：同一 submission_id 下的多个 approval_batch 属于同一次
publish_requirements 提交，便于审批队列按组展示与批量处理。

迁移策略（共享库在线）：
- nullable 且无 server_default → PG 11+ ADD COLUMN 不重写表；
- 存量批次保持 NULL（单条工具行为不变）；
- submission_id 仅做分组，不参与现有幂等键
  (submitted_by, batch_type, operation_id)。

Revision ID: 0005_batch_submission_id
Revises: 0004_access_key_name_owner
Create Date: 2026-11-24 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0005_batch_submission_id'
down_revision: Union[str, Sequence[str], None] = '0004_access_key_name_owner'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """新增 submission_id 列与索引（nullable，在线加列不重写表）。"""
    op.add_column(
        'approval_batch',
        sa.Column(
            'submission_id',
            sa.UUID(as_uuid=True),
            nullable=True,
            comment='批量提交分组 ID（publish_requirements 同一批共享；单条工具为 NULL）',
        ),
    )
    op.create_index(
        'idx_approval_batch_submission',
        'approval_batch',
        ['submission_id'],
    )


def downgrade() -> None:
    """回退：删除 submission_id 列与索引（分组信息随之丢失）。"""
    op.drop_index('idx_approval_batch_submission', table_name='approval_batch')
    op.drop_column('approval_batch', 'submission_id')
