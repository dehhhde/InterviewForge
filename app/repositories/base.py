"""泛型 BaseRepository：get_by_id/list/create/update/delete〔阶段 3 · §6.4〕

四条约定，子类别再各自发明：

1. **repository 不 commit**。它只 `flush`（为了拿到 Python 侧默认值生成的 `id`，并让约束错误
   当场暴露），提交时机由调用方用 `app.utils.db.session_scope` 决定。一次"落一份报告"要写
   `interview` + N 行 `question`，它们必须落在同一个事务里，否则中途失败就是半份报告——
   而 §4.6 要求的幂等重跑正好依赖"要么全有要么全无"。
2. **`get_by_id` 找不到就返回 None，不抛异常**。404 是 API 层的语义，repository 不知道
   自己在服务 HTTP 请求；把 `NotFoundError` 抛在这里，后台任务和脚本就得跟着伪造 HTTP 语境。
3. **`order_by` 走列名白名单**（`model_class.column_names()`）。这是唯一一处"字符串来自调用方
   却参与 SQL 构造"的地方，白名单就是它的边界。
4. **只返回 ORM 实例，不返回 dict**。摊平成 DTO 是 service/schema 层的活儿；
   repository 一开始 `model_dump`，分层就变成两层了。

没有 `relationship()`，所以也没有"顺手 join 一把"的能力：跨表读取要在子类里显式写 join
（见 `question_repository.list_for_interview`）。这是刻意的——读了几张表得能在代码里数出来。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import ORMModel


class BaseRepository[ModelT: ORMModel]:
    """单表 CRUD。子类只需绑定 `model_class`，再补自己的自然键查询。"""

    #: 子类里写成 `model_class: ClassVar[type[Resume]] = Resume`。
    #: 绑成 `type[ModelT]` 而不是 `type[ORMModel]`，否则 `get_by_id` 的返回类型
    #: 会被推成 `ORMModel | None`，四个子类的调用点全都要写 cast。
    model_class: ClassVar[type[ModelT]]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, id_: str) -> ModelT | None:
        return await self.session.get(self.model_class, id_)

    async def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        order_by: str = "created_at",
        desc: bool = True,
        filters: Sequence[ColumnElement[bool]] = (),
    ) -> Sequence[ModelT]:
        """分页读。`filters` 收 SQL 条件（`Resume.name == "…"` 这种），不拼字符串。"""
        allowed = self.model_class.column_names()
        if order_by not in allowed:
            raise ValueError(f"{self.model_class.__name__} 无列 {order_by!r}，可用：{allowed}")
        column = getattr(self.model_class, order_by)
        stmt = select(self.model_class)
        for condition in filters:
            stmt = stmt.where(condition)
        stmt = stmt.order_by(column.desc() if desc else column.asc()).limit(limit).offset(offset)
        return (await self.session.execute(stmt)).scalars().all()

    async def count(self, *, filters: Sequence[ColumnElement[bool]] = ()) -> int:
        stmt = select(func.count()).select_from(self.model_class)
        for condition in filters:
            stmt = stmt.where(condition)
        return int((await self.session.execute(stmt)).scalar_one())

    async def create(self, **values: Any) -> ModelT:
        """插一行并 flush。`id` / `created_at` 是 Python 侧默认值，flush 时才算出来。"""
        instance = self.model_class(**values)
        self.session.add(instance)
        await self.session.flush()
        return instance

    async def update(self, id_: str, **values: Any) -> ModelT | None:
        """按 id 改指定列。`updated_at` 由 `TimestampMixin.onupdate` 自动盖，别手工传。"""
        instance = await self.get_by_id(id_)
        if instance is None:
            return None
        for key, value in values.items():
            if key not in self.model_class.column_names():
                raise ValueError(f"{self.model_class.__name__} 没有列 {key!r}")
            setattr(instance, key, value)
        await self.session.flush()
        return instance

    async def delete(self, id_: str) -> bool:
        """按 id 删。返回是否真删了——调用方要区分"删掉了"和"本来就没有"。"""
        instance = await self.get_by_id(id_)
        if instance is None:
            return False
        await self.session.delete(instance)
        await self.session.flush()
        return True
