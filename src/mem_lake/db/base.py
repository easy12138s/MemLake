"""SQLAlchemy Base 声明基类。

所有模块的 ORM 模型（knowledge/models.py、auth/models.py、approval/models.py、audit/models.py）
均继承此 Base，共享同一 metadata，支持 Base.metadata.create_all() 统一建表。
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 ORM 模型的声明基类。"""

    pass
