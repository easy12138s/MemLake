"""知识图谱存储模块：Schema 校验 + ORM 模型 + GraphStore 抽象 + Repository 业务接口。

公共接口导出（供 gateway/approval 等上层模块使用）：
- Schema 校验：NODE_TYPES、EDGE_TYPES、validate_node、validate_edge_type、SchemaValidationError
- Repository 业务：create_node、get_node、update_node、archive_node、add_edge、
  regenerate_vector、list_nodes_by_project、NodeNotFoundError
- 图存储抽象与实现：GraphStore、AGEGraphStore、get_graph_store
- ORM 模型：KnowledgeNode
"""

from mem_lake.knowledge.age_store import AGEGraphStore, get_graph_store
from mem_lake.knowledge.graph_store import GraphStore
from mem_lake.knowledge.models import KnowledgeNode
from mem_lake.knowledge.repository import (
    NodeNotFoundError,
    add_edge,
    archive_node,
    create_node,
    get_node,
    get_nodes_by_ids,
    list_nodes_by_project,
    regenerate_vector,
    update_node,
)
from mem_lake.knowledge.schema import (
    EDGE_TYPES,
    NODE_TYPES,
    SchemaValidationError,
    validate_edge_type,
    validate_node,
)

__all__ = [
    # Schema
    "NODE_TYPES",
    "EDGE_TYPES",
    "validate_node",
    "validate_edge_type",
    "SchemaValidationError",
    # Models
    "KnowledgeNode",
    # GraphStore
    "GraphStore",
    "AGEGraphStore",
    "get_graph_store",
    # Repository
    "create_node",
    "get_node",
    "get_nodes_by_ids",
    "update_node",
    "archive_node",
    "add_edge",
    "regenerate_vector",
    "list_nodes_by_project",
    "NodeNotFoundError",
]
