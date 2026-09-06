"""drop knowledge_node.content_vector column

FIX-08：废弃 knowledge_node.content_vector 列。

背景：检索主路径已走 node_embedding 表（search/vector.py 的 NodeEmbedding 多向量
max-pooling），knowledge_node.content_vector 全仓读点仅审计 flag，且 HNSW 索引
维护为空转开销。本迁移 DROP idx_node_vector 索引与 content_vector 列（models.py
已同步删列定义，避免 create_all 与迁移不一致）。

升级后存量 content_vector 数据随列删除（语义：该列自始即非检索主路径，无
迁移数据价值）。

Revision ID: 0003_drop_content_vector
Revises: 0002_fix_vector_index_opclass
Create Date: 2026-09-06 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0003_drop_content_vector'
down_revision: Union[str, Sequence[str], None] = '0002_fix_vector_index_opclass'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """删除 knowledge_node.content_vector 列及其 HNSW 索引。"""
    op.drop_index('idx_node_vector', table_name='knowledge_node')
    op.drop_column('knowledge_node', 'content_vector')


def downgrade() -> None:
    """回退：恢复 content_vector 列与 idx_node_vector 索引（vector_ip_ops）。"""
    op.add_column(
        'knowledge_node',
        sa.Column('content_vector', sa.dialects.postgresql.VECTOR(1024), nullable=True),
    )
    op.create_index(
        'idx_node_vector',
        'knowledge_node',
        ['content_vector'],
        unique=False,
        postgresql_using='hnsw',
        postgresql_with={'m': 32, 'ef_construction': 400},
        postgresql_ops={'content_vector': 'vector_ip_ops'},
    )
