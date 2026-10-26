"""ORM 模型层出口〔阶段 3〕

`alembic autogenerate` 只看得见**已被 import** 的模型，所以这里必须登记每张表：
新增模型忘了 import，生成的 migration 就会缺表，而且迁移照样"成功"，
直到运行期 `no such table` 才炸。这条约定值一个 docstring。

`user.py` 是空壳，故有意不在这里出现——认证是阶段 2 的活儿，
本阶段四张表之间不存在任何 `user_id` 外键（欠账已记进 `docs/TODO.md`）。
"""

from app.models.base import Base, ORMModel, PrimaryKeyMixin, TimestampMixin, utcnow_naive
from app.models.interview import Interview
from app.models.job import Job
from app.models.question import Question
from app.models.resume import Resume
from app.models.task import Task

__all__ = [
    "Base",
    "Interview",
    "Job",
    "ORMModel",
    "PrimaryKeyMixin",
    "Question",
    "Resume",
    "Task",
    "TimestampMixin",
    "utcnow_naive",
]
