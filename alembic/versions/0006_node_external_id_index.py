"""add expression index for knowledge_node.properties->>'external_id'

需求关系批量解析（update_requirement_relations）按 external_id 反查节点，
原实现只能全表扫描。本迁移只新增表达式索引，不改列、不回填数据。

Revision ID: 0006_node_external_id_index
Revises: 0005_batch_submission_id
Create Date: 2026-11-26 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_node_external_id_index"
down_revision: Union[str, Sequence[str], None] = "0005_batch_submission_id"


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_node_external_id "
        "ON knowledge_node ((properties->>'external_id'))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_node_external_id")
