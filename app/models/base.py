"""DeclarativeBase + id/created_at/updated_at mixin〔阶段 3 · §6.3〕

三条刻意的取舍，改之前先读懂：

1. **主键是 `String(32)` 的 uuid4 hex，不是自增 int。** 简历 id 由 API 在落盘 PDF 时就生成，
   图跑完再回写；自增 id 要到 insert 后才有值，拿它当 `AgentState.resume_id` 会让上传与
   工作流之间多一次往返。hex 去掉了连字符，省 4 个字符也让索引紧凑。
2. **时间戳在 Python 侧默认，不用 `server_default=func.now()`。** 服务端默认值要 flush 后
   回读，异步下触发 `MissingGreenlet`；`created_at` 的精度对本项目只有"排序"这一种用法，
   不值得为它引入 eager loaded 的复杂度。存的是 naive UTC——SQLite 的 `DateTime` 不存时区，
   混用 aware 值会静默变成字符串比较。
3. **不用 ORM `relationship()`。** 四张表之间只留外键列 + 索引，读取由 repository 显式 join。
   `relationship()` 的懒加载在 asyncio 下同样是 `MissingGreenlet`，而且会把"读几张表"这件事
   藏进属性访问里，违反分层铁律里"repository 是唯一出口"的初衷。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import DateTime, MetaData, String
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

#: SQLite 不支持 `ALTER TABLE ... RENAME CONSTRAINT`，命名约定让 alembic 生成的
#: 约束名可预期，将来要删/改约束时 `op.drop_constraint("名字")` 才写得出来。
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow_naive() -> datetime:
    """当前 UTC 时间，去掉时区标记。

    见模块 docstring 第 2 条：库里存的就是 naive UTC，比较和排序都按这个口径来。
    """
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    """所有 ORM 模型的共同基类。

    `__tablename__` 在每个模型里显式写死：SQLAlchemy 2.1 在这个版本上没有可靠的类名推导
    （缺 `__tablename__` 直接 `InvalidRequestError`），而显式命名让 migration、
    手写 SQL 和 repository 里的 join 引用的是同一个字符串。
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)  # type: ignore[arg-type]


class PrimaryKeyMixin:
    """`String(32)` uuid 主键。"""

    @declared_attr
    def id(cls) -> Mapped[str]:
        return mapped_column(String(32), primary_key=True, default=lambda: uuid4().hex)


class TimestampMixin:
    """`created_at` / `updated_at`，都在 Python 侧取值。"""

    @declared_attr
    def created_at(cls) -> Mapped[datetime]:
        return mapped_column(DateTime, default=utcnow_naive, nullable=False)

    @declared_attr
    def updated_at(cls) -> Mapped[datetime]:
        return mapped_column(DateTime, default=utcnow_naive, onupdate=utcnow_naive, nullable=False)


class ORMModel(Base, PrimaryKeyMixin, TimestampMixin):
    """业务表的公共形状：id + 两个时间戳。

    repository 的泛型参数以它为界（`BaseRepository[Resume]`），所以四张表都得继承这个类，
    继承 `Base` 本身会让泛型的 `id` / `created_at` 访问失去类型信息。
    """

    __abstract__ = True

    @classmethod
    def column_names(cls) -> tuple[str, ...]:
        """真实列名白名单。

        `BaseRepository.list(order_by=...)` 只认这里的名字：把请求里的字符串直接
        `getattr(model, name)` 会变成注入面（`__table__`、`metadata` 都摸得到）。
        """
        return tuple(c.key for c in cls.__table__.columns)
