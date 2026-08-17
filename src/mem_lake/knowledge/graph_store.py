"""GraphStore 抽象接口：add_node、add_edge、neighbors、find_path、match_pattern、subgraph、delete_node。

对齐 PDD 10.2 GraphStore 抽象层。定义图操作原语，AGEGraphStore 为 v1.0 实现，
未来可替换为 Neo4j 等其他图后端，业务代码无感。
"""

import uuid
from abc import ABC, abstractmethod
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


class EdgeTargetNotFoundError(Exception):
    """边端点节点在图中不存在时抛出。

    AGE 的 MATCH ... CREATE 在 MATCH 失败时静默跳过（Cypher 标准行为），
    实现层必须在创建后校验结果，端点缺失时抛出本异常触发调用方事务回滚。
    """


class GraphStore(ABC):
    """图存储抽象基类，定义图操作原语。

    接口契约：
    - properties 永远含 project_id 用于图内项目隔离
    - 返回的 dict 为 agtype 解析后的普通 dict（含 id/label/properties 键）
    - 所有方法均在传入的 session 事务内执行，不 commit
    """

    @abstractmethod
    async def add_node(
        self,
        session: AsyncSession,
        node_id: uuid.UUID,
        label: str,
        properties: dict,
    ) -> None:
        """添加节点。label 为节点类型，properties 必须含 id 与 project_id。"""

    @abstractmethod
    async def add_edge(
        self,
        session: AsyncSession,
        from_id: uuid.UUID,
        to_id: uuid.UUID,
        edge_type: str,
        properties: dict,
    ) -> None:
        """添加边。edge_type 为关系类型，properties 携带边元数据。

        端点节点不存在时抛 EdgeTargetNotFoundError（禁止静默丢边）。
        """

    @abstractmethod
    async def neighbors(
        self,
        session: AsyncSession,
        node_id: uuid.UUID,
        edge_type: str | None = None,
        depth: int = 1,
    ) -> list[dict]:
        """邻居遍历。返回邻居节点 dict 列表。edge_type=None 表示不限类型。"""

    @abstractmethod
    async def find_path(
        self,
        session: AsyncSession,
        from_id: uuid.UUID,
        to_id: uuid.UUID,
        max_depth: int = 5,
    ) -> list[list[dict]]:
        """路径查询。返回路径列表，每条路径为节点 dict 列表。无路径返回空。"""

    @abstractmethod
    async def match_pattern(
        self,
        session: AsyncSession,
        pattern: str,
        params: dict | None = None,
    ) -> list[dict]:
        """图模式匹配。pattern 为 Cypher MATCH 子句（受信任调用方构造）。"""

    @abstractmethod
    async def subgraph(
        self,
        session: AsyncSession,
        node_ids: list[uuid.UUID],
    ) -> dict[str, Any]:
        """子图提取。返回 {"nodes": [...], "edges": [...]}。"""

    @abstractmethod
    async def delete_node(
        self,
        session: AsyncSession,
        node_id: uuid.UUID,
    ) -> None:
        """删除节点及其关联边（DETACH DELETE）。幂等。"""
