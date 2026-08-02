"""依赖注入工厂：为工具函数提供 DB 会话、当前角色、项目权限校验等依赖。

FastMCP 4.0 依赖注入机制：
- 工具函数参数用 `Depends(dependency_func)` 声明依赖
- `CurrentAccessToken()` 直接获取当前 AccessToken（已由 AccessKeyAuthMiddleware 设置）
- 自定义依赖函数可 yield（context manager 模式）或直接返回值

本模块采用直接调用方式（非 Depends 装饰器），因为：
1. 事务边界控制需要明确的 try/except/finally 结构，yield 依赖难以表达
2. lifespan 资源通过 `get_context().lifespan_context` 获取，已足够清晰
3. 直接调用更易测试（无需 Mock Depends 机制）

对齐 PDD 3.1：工具层控制事务边界，service 层不 commit。
"""

import logging
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from fastmcp.server.dependencies import get_access_token
from sqlalchemy.ext.asyncio import AsyncSession

from mem_lake.db.session import AsyncSessionLocal

logger = logging.getLogger("mem_lake.gateway.dependencies")


def get_current_access_token() -> AccessToken:
    """获取当前请求的 AccessToken。

    AccessKeyAuthMiddleware 已将 AccessToken 设置到 request.scope["user"]，
    get_access_token() 会从中读取。

    返回：当前 AccessToken
    抛出 ToolError：未认证（未提供或无效 Access Key）
    """
    token = get_access_token()
    if token is None:
        raise ToolError("未认证：缺少有效的 Access Key（X-MCP-Key 头）")
    return token


def get_current_key_id() -> str:
    """获取当前调用者的 Access Key ID。

    返回：key_id 字符串（用于 submitted_by / reviewed_by / actor 等字段）
    """
    token = get_current_access_token()
    key_id = token.claims.get("key_id")
    if not key_id:
        # 兜底：用 client_id（同样是 key_id 的字符串形式）
        key_id = token.client_id
    return str(key_id)


def get_current_role() -> str:
    """获取当前调用者的角色（admin/pm/dev）。

    返回：角色字符串
    """
    token = get_current_access_token()
    role = token.claims.get("role")
    if not role:
        # 兜底：从 scopes 读取（AccessKeyAuthMiddleware 同时设置了 scopes=[role]）
        if token.scopes:
            role = token.scopes[0]
    if not role:
        raise ToolError("认证信息缺少角色 claims")
    return str(role)


def get_current_project_scope() -> list[str]:
    """获取当前调用者的项目范围（项目 ID 字符串列表）。

    admin 角色返回空列表（不受限），pm/dev 角色返回其项目范围。
    """
    token = get_current_access_token()
    scope = token.claims.get("project_scope", [])
    return [str(pid) for pid in scope] if scope else []


def validate_project_access(project_id: uuid.UUID) -> None:
    """校验当前调用者是否有权访问指定项目。

    PDD 3.1：admin 角色不受项目范围限制；pm/dev 角色只能访问 project_scope 内的项目。

    参数：
        project_id: 要访问的项目 ID

    抛出 ToolError：无权访问该项目
    """
    token = get_current_access_token()
    role = token.claims.get("role", "")

    # admin 不受项目范围限制
    if role == "admin":
        return

    # pm/dev 校验项目范围
    scope = token.claims.get("project_scope", []) or []
    scope_str = [str(pid) for pid in scope]
    if str(project_id) not in scope_str:
        raise ToolError(
            f"权限拒绝：项目 {project_id} 不在当前 Access Key 的项目范围内"
        )


@asynccontextmanager
async def transactional_session() -> AsyncIterator[AsyncSession]:
    """事务边界控制上下文管理器。

    PDD 硬约束：工具层控制事务边界，service 层不 commit。
    工具函数用法：
        async with transactional_session() as session:
            batch = await submit_batch(session, ...)
            # service 层不 commit，由本上下文管理器统一提交

    成功时 commit，异常时 rollback。异常会向上抛出（不吞掉）。
    """
    session = AsyncSessionLocal()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_readonly_session() -> AsyncSession:
    """获取只读会话（不自动 commit）。

    用于读工具（review_pending_list / review_batch_detail / get_role_skills），
    这些工具不需要事务，只需读取数据。

    调用方负责在 finally 中 close session。
    """
    return AsyncSessionLocal()
