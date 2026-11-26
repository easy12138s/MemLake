"""审批 payload / 结构校验。

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
        # schema.validate_attribution（单一实现），保留 title/content/created_by 校验。
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

    elif item_type == "node" and action == "update":
        if not payload.get("node_id"):
            raise PayloadValidationError(f"item[{idx}] node+update payload 缺 node_id")
        change_keys = (
            "title",
            "content",
            "properties",
            "tags",
            "source",
            "tags_add",
            "tags_remove",
            "properties_patch",
        )
        has_change = any(payload.get(key) is not None for key in change_keys)
        if "project_id" in payload:
            # 归属变更/解绑（bind_requirement）：project_id 允许为 None
            has_change = True
        if not has_change:
            raise PayloadValidationError(
                f"item[{idx}] node+update 至少提供一个变更字段"
            )
        if "properties" in payload and "properties_patch" in payload:
            raise PayloadValidationError(
                f"item[{idx}] properties 与 properties_patch 互斥"
            )
        base_version = payload.get("base_version")
        if base_version is not None and (
            not isinstance(base_version, int) or base_version < 1
        ):
            raise PayloadValidationError(
                f"item[{idx}] base_version 必须为正整数"
            )
        for key in ("tags_add", "tags_remove"):
            if key in payload and not isinstance(payload[key], list):
                raise PayloadValidationError(f"item[{idx}] {key} 必须为数组")
        if "properties_patch" in payload and not isinstance(
            payload["properties_patch"], dict
        ):
            raise PayloadValidationError(
                f"item[{idx}] properties_patch 必须为对象"
            )

    # node + update / edge + update / delete：提交时不强校验，留待审批通过时校验
