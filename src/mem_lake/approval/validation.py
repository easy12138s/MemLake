"""审批 payload / 结构校验（FIX-24 从 service.py 拆出的 validation 模块）。

职责：提交批次时对 items 的结构（_validate_item_structure）与 payload 的
合规性（_validate_item_payload）做校验。校验失败抛 PayloadValidationError。

归属约束（Requirement 必填 system_id、其余必填 project_id）复用
knowledge/schema.validate_attribution；节点/边类型校验复用 schema 层，保证单一实现。
"""

from typing import Any

from mem_lake.knowledge.schema import (
    SchemaValidationError,
    validate_attribution,
    validate_edge_type,
    validate_node,
)


class PayloadValidationError(Exception):
    """payload 结构不合规时抛出。"""


def _validate_item_structure(item: dict[str, Any], idx: int) -> None:
    """校验 item 结构含必要字段。"""
    required_keys = {"item_type", "action", "entity_type", "payload"}
    missing = required_keys - set(item.keys())
    if missing:
        raise PayloadValidationError(
            f"item[{idx}] 缺失字段: {sorted(missing)}，必要字段: {sorted(required_keys)}"
        )

    if item["item_type"] not in ("node", "edge"):
        raise PayloadValidationError(
            f"item[{idx}] 非法 item_type: {item['item_type']}，合法值: node/edge"
        )

    if item["action"] not in ("create", "update", "delete"):
        raise PayloadValidationError(
            f"item[{idx}] 非法 action: {item['action']}，合法值: create/update/delete"
        )


def _validate_item_payload(item: dict[str, Any], idx: int) -> None:
    """校验 payload 合规性（提交时校验，避免审批通过时才发现错误）。"""
    item_type = item["item_type"]
    action = item["action"]
    entity_type = item["entity_type"]
    payload = item["payload"]

    if not isinstance(payload, dict):
        raise PayloadValidationError(f"item[{idx}] payload 必须为 dict")

    if item_type == "node" and action == "create":
        # node + create：校验节点类型、必填字段与必填顶层字段。
        # 归属约束（Requirement 必填 system_id、其余必填 project_id）统一走
        # schema.validate_attribution（FIX-17 单一实现），保留 title/content/created_by 校验。
        required_top = ("title", "content", "created_by")
        for required in required_top:
            if not payload.get(required):
                raise PayloadValidationError(
                    f"item[{idx}] node+create payload 缺必填字段: {required}"
                )
        try:
            validate_attribution(
                entity_type,
                system_id=payload.get("system_id"),
                project_id=payload.get("project_id"),
            )
        except SchemaValidationError as e:
            raise PayloadValidationError(f"item[{idx}] node+create 归属校验失败: {e}") from e
        properties = payload.get("properties")
        if not isinstance(properties, dict):
            raise PayloadValidationError(
                f"item[{idx}] node+create payload 缺 properties 或非 dict"
            )
        try:
            validate_node(entity_type, properties)
        except SchemaValidationError as e:
            raise PayloadValidationError(f"item[{idx}] node+create 校验失败: {e}") from e

    elif item_type == "edge" and action == "create":
        # edge + create：校验边类型
        try:
            validate_edge_type(entity_type)
        except SchemaValidationError as e:
            raise PayloadValidationError(f"item[{idx}] edge+create 校验失败: {e}") from e

        # 校验 from_ref/to_ref 存在（支持批次内临时引用，PDD 5.3）
        has_from = "from_ref" in payload
        has_to = "to_ref" in payload
        if not has_from or not has_to:
            raise PayloadValidationError(
                f"item[{idx}] edge+create payload 缺 from_ref/to_ref"
            )

    # node + update / edge + update / delete：提交时不强校验，留待审批通过时校验
