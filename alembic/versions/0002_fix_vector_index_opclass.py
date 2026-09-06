"""fix vector index opclass to vector_ip_ops

FIX-04：统一向量索引 opclass。

背景：deploy 库中 idx_node_vector / idx_node_embedding_vector 实际为
vector_cosine_ops（create_all 跳过已存在索引、无迁移机制所致），而 models.py
期望 vector_ip_ops。cosine_ops 索引不服务 <#>（max_inner_product）查询，
部署库向量检索实际走顺序扫描（FIX-01 迁移机制缺失的现实病例佐证）。

本迁移 DROP 两个 HNSW 索引并以 vector_ip_ops（参数 m=32、ef_construction=400
保持）重建，与 models.py __table_args__ 的期望一致。

Revision ID: 0002_fix_vector_index_opclass
Revises: 0001_initial
Create Date: 2026-09-06 11:30:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0002_fix_vector_index_opclass'
down_revision: Union[str, Sequence[str], None] = '0001_initial'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# HNSW 索引参数与 models.py __table_args__ 保持一致（m=32、ef_construction=400）
_HNSW_WITH = {'m': 32, 'ef_construction': 400}
_IP_OPS = {'content_vector': 'vector_ip_ops'}


def upgrade() -> None:
    """重建两个向量索引为 vector_ip_ops。"""
    # knowledge_node.content_vector（全表，可能存有命中节点）
    op.drop_index(
        'idx_node_vector',
        table_name='knowledge_node',
    )
    op.create_index(
        'idx_node_vector',
        'knowledge_node',
        ['content_vector'],
        unique=False,
        postgresql_using='hnsw',
        postgresql_with=_HNSW_WITH,
        postgresql_ops=_IP_OPS,
    )
    # node_embedding.content_vector
    op.drop_index(
        'idx_node_embedding_vector',
        table_name='node_embedding',
    )
    op.create_index(
        'idx_node_embedding_vector',
        'node_embedding',
        ['content_vector'],
        unique=False,
        postgresql_using='hnsw',
        postgresql_with=_HNSW_WITH,
        postgresql_ops=_IP_OPS,
    )


def downgrade() -> None:
    """回退：重建为原 create_all 缺省 opclass（vector_cosine_ops）。

    仅供回滚参考；新部署与修复后的库均以 vector_ip_ops 为准（FIX-04 基线）。
    """
    op.drop_index('idx_node_embedding_vector', table_name='node_embedding')
    op.create_index(
        'idx_node_embedding_vector',
        'node_embedding',
        ['content_vector'],
        unique=False,
        postgresql_using='hnsw',
        postgresql_with=_HNSW_WITH,
    )
    op.drop_index('idx_node_vector', table_name='knowledge_node')
    op.create_index(
        'idx_node_vector',
        'knowledge_node',
        ['content_vector'],
        unique=False,
        postgresql_using='hnsw',
        postgresql_with=_HNSW_WITH,
    )