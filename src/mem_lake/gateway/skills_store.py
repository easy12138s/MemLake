"""角色技能文档读取（MCP Resource 与 get_skill 工具共用）。

技能文档随镜像分发（`src/mem_lake/skills/{role}/{SKILL,REFERENCE}.md`），
不再要求 Agent 从 GitHub 下载：同一 MCP 连接、同一 `X-MCP-Key` 鉴权按需读取。

本模块不依赖 FastMCP，便于纯逻辑测试；资源/工具层只做参数解析与鉴权入口。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from mem_lake.auth.rbac import VALID_ROLES

# 文件白名单：URI 里用短名，落盘用带扩展名的文件名
SKILL_FILES: dict[str, str] = {"SKILL": "SKILL.md", "REFERENCE": "REFERENCE.md"}


class SkillDocError(ValueError):
    """技能文档读取失败（未知角色/文件或越权）。"""


@dataclass(frozen=True)
class SkillDoc:
    """一份角色技能文档。"""

    role: str
    file: str
    version: str
    sha256: str
    content: str

    @property
    def uri(self) -> str:
        return f"memlake://skills/{self.role}/{self.file}"


def skills_root() -> Path:
    """技能文档根目录（随包分发的 src/mem_lake/skills/）。"""
    return Path(__file__).resolve().parent.parent / "skills"


def normalize_file(file: str) -> str:
    """把 SKILL / SKILL.md / reference 归一为白名单短名。"""
    key = (file or "").strip().upper()
    if key.endswith(".MD"):
        key = key[:-3]
    if key not in SKILL_FILES:
        raise SkillDocError(f"未知技能文件: {file}，可选: {sorted(SKILL_FILES)}")
    return key


def _parse_version(text: str) -> str:
    """读取 YAML frontmatter 的 version；无 frontmatter/解析失败返回 0.0.0。"""
    if not text.startswith("---"):
        return "0.0.0"
    _, _, rest = text.partition("---")
    front, _, _ = rest.partition("---")
    try:
        data = yaml.safe_load(front) or {}
    except yaml.YAMLError:
        return "0.0.0"
    if not isinstance(data, dict):
        return "0.0.0"
    return str(data.get("version", "0.0.0"))


def read_skill_doc(role: str, file: str, *, caller_role: str) -> SkillDoc:
    """读取技能文档并校验角色可见性。

    - role 必须在 VALID_ROLES 内
    - 非 admin 只能读自己角色（admin 可读全部）
    """
    if role not in VALID_ROLES:
        raise SkillDocError(f"未知角色: {role}，可选: {sorted(VALID_ROLES)}")
    if caller_role != "admin" and role != caller_role:
        raise SkillDocError(f"无权读取其他角色技能: {role}（当前角色 {caller_role}）")

    key = normalize_file(file)
    path = skills_root() / role / SKILL_FILES[key]
    if not path.is_file():
        raise SkillDocError(f"技能文件不存在: {path.name}（角色 {role}）")

    content = path.read_text(encoding="utf-8")
    return SkillDoc(
        role=role,
        file=key,
        version=_parse_version(content),
        sha256=hashlib.sha256(content.encode("utf-8")).hexdigest()[:16],
        content=content,
    )


def list_skill_entries(*, caller_role: str) -> list[dict[str, Any]]:
    """当前角色可读的技能索引（admin 列三角色，其他角色只列自己）。"""
    roles = sorted(VALID_ROLES) if caller_role == "admin" else [caller_role]
    entries: list[dict[str, Any]] = []
    for role in roles:
        for key in SKILL_FILES:
            try:
                doc = read_skill_doc(role, key, caller_role=caller_role)
            except SkillDocError:
                continue
            entries.append(
                {
                    "role": doc.role,
                    "file": doc.file,
                    "version": doc.version,
                    "sha256": doc.sha256,
                    "uri": doc.uri,
                }
            )
    return entries
