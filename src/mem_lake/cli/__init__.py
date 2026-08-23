"""admin 引导 CLI：创建第一个 admin Access Key。

仅复用 auth/service.create_access_key + db/session.AsyncSessionLocal，
不直写 SQL。明文仅 stdout 输出一次。

部署环境执行：
    docker exec -it deploy-mem-lake-1 memlake-bootstrap-admin
    # 等价: docker exec -it deploy-mem-lake-1 python -m mem_lake.cli
"""

import asyncio

from mem_lake.auth.service import create_access_key
from mem_lake.db.session import AsyncSessionLocal


async def main() -> None:
    """创建 admin Access Key（project_scope 为空 = 不限项目）并提交事务。"""
    async with AsyncSessionLocal() as session:
        key_id, plaintext = await create_access_key(
            session,
            role="admin",
            project_scope=[],
            created_by="system",
        )
        await session.commit()

    print(f"创建成功: key_id={key_id}")
    print(f"Access Key: {plaintext}")
    print("请立即保存，明文仅显示这一次。")


def run() -> None:
    """console_scripts 入口（memlake-bootstrap-admin）。"""
    asyncio.run(main())


if __name__ == "__main__":
    run()
