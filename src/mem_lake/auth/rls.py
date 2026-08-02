"""PostgreSQL 行级安全辅助：SET LOCAL 注入 project_id / actor 上下文。

提供事务级会话变量注入，供 M3 RLS 策略引用（current_setting('app.current_project_id')）。
使用 set_config(name, value, true) 函数实现事务级设置（第三参数 true 等价 SET LOCAL 语义），
支持参数化绑定；SET LOCAL 语句本身不支持 $1 占位符。
RLS 策略 SQL（CREATE POLICY）由 M3 的 knowledge 建表逻辑执行。
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def set_project_context(session: AsyncSession, project_id: uuid.UUID | str) -> None:
    """注入当前事务的 project_id 上下文。"""
    pid = str(project_id)
    await session.execute(
        text("SELECT set_config('app.current_project_id', :pid, true)"),
        {"pid": pid},
    )


async def set_actor_context(session: AsyncSession, actor: str) -> None:
    """注入当前事务的 actor 上下文。"""
    await session.execute(
        text("SELECT set_config('app.current_actor', :actor, true)"),
        {"actor": actor},
    )


async def get_project_context(session: AsyncSession) -> str | None:
    """读取当前事务的 project_id 上下文。不存在返回 None。"""
    result = await session.execute(
        text("SELECT current_setting('app.current_project_id', true)")
    )
    val = result.scalar()
    return val if val else None


async def get_actor_context(session: AsyncSession) -> str | None:
    """读取当前事务的 actor 上下文。不存在返回 None。"""
    result = await session.execute(
        text("SELECT current_setting('app.current_actor', true)")
    )
    val = result.scalar()
    return val if val else None


async def clear_context(session: AsyncSession) -> None:
    """清除当前事务的 project_id 与 actor 上下文。"""
    await session.execute(text("RESET app.current_project_id"))
    await session.execute(text("RESET app.current_actor"))
