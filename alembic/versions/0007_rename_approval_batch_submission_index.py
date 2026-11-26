"""align approval_batch submission index name with the deployed migration

0005 建的是 `idx_approval_batch_submission`，而 ORM 的 `index=True` 生成
`ix_approval_batch_submission_id`。本迁移只做索引改名/补齐，无列变更、
无数据回写：
- 由 `create_all` 建的库（ix_）→ 改名为 idx_（与 0005/线上一致）；
- 正常 alembic 升级的库（已是 idx_）→ 保持不动（幂等）。

Revision ID: 0007_rename_approval_batch_submission_index
Revises: 0006_node_external_id_index
Create Date: 2026-11-26 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_rename_approval_batch_submission_index"
down_revision: Union[str, Sequence[str], None] = "0006_node_external_id_index"


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_indexes
                WHERE indexname = 'ix_approval_batch_submission_id'
            ) AND NOT EXISTS (
                SELECT 1 FROM pg_indexes
                WHERE indexname = 'idx_approval_batch_submission'
            ) THEN
                ALTER INDEX ix_approval_batch_submission_id
                    RENAME TO idx_approval_batch_submission;
            END IF;
        END $$;
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_approval_batch_submission "
        "ON approval_batch (submission_id)"
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_indexes
                WHERE indexname = 'idx_approval_batch_submission'
            ) AND NOT EXISTS (
                SELECT 1 FROM pg_indexes
                WHERE indexname = 'ix_approval_batch_submission_id'
            ) THEN
                ALTER INDEX idx_approval_batch_submission
                    RENAME TO ix_approval_batch_submission_id;
            END IF;
        END $$;
        """
    )
