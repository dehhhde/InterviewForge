"""异步任务进度模型〔阶段 8 · M6〕

`BackgroundTasks` 跑在**同一个进程**里，但 API 响应必须先返回，客户端只能靠
`GET /tasks/{task_id}` 轮询——"进度写库"要落的就是这张表：一次后台执行 = 一行，
每次换状态就 `update`，`updated_at` 由 mixin 自动盖戳，轮询方读它就能看出任务活着。

为什么不复用 `interview.status`：`resume_parsing` 的产物是 `resume` 行，一次上传之后
才可能产生报告头，把"解析进度"塞进报告状态会逼 API 在还没决定要不要出题时就插
`interview` 行。一张 `task` 表装两种任务，`kind` 区分，`ref_id` 指向各自的业务行。

`progress` / `result` / `error` 用 JSON 而不是拆列：形状由任务自己决定（步骤列表、
题数汇总、错误码），API 只透传给轮询方，从来没有人按它们查——按列查进度是第二份真相。
"""

from __future__ import annotations

from typing import Any, ClassVar

from sqlalchemy import JSON, CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import ORMModel

#: 任务类型。与 `app/tasks/` 的两个模块一一对应。
KIND_RESUME_PARSING = "resume_parsing"
KIND_QUESTION_GENERATION = "question_generation"
TASK_KINDS: tuple[str, ...] = (KIND_RESUME_PARSING, KIND_QUESTION_GENERATION)

#: 生命周期。`pending` 是 API 建行的初始态（还没排上后台执行），
#: `running` 由任务自己翻，`completed` / `failed` 是终局。
TASK_STATUSES: tuple[str, ...] = ("pending", "running", "completed", "failed")


class Task(ORMModel):
    """一次后台任务的进度记录，API 轮询的唯一读数点。"""

    __tablename__ = "task"

    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )
    #: 业务行 id：`resume_parsing` 指 `resume.id`，`question_generation` 指 `interview.id`。
    #: 不做外键——任务行要活得比业务行长（业务行被删后进度仍要可读），而且两种 kind
    #: 指向两张表，单列挂不了 `ForeignKey`。
    ref_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    #: `{"steps": [{"node": "parse", "ms": 12.3}, …]}`，由 task_service 追加。
    progress: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    #: 成功收尾的摘要（如题数、批次）；失败时为空。
    result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    #: 失败时写 `{code, message, details}`；`details` 只放排障字段，不放密钥。
    error: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    #: `kind` 取值域常量，repository 用它挡拼写错误（同 `set_status` 的理由）。
    KINDS: ClassVar[tuple[str, ...]] = TASK_KINDS
    STATUSES: ClassVar[tuple[str, ...]] = TASK_STATUSES

    __table_args__ = (
        # name 只给后缀：`NAMING_CONVENTION["ck"]` 会拼成 `ck_task_kind` / `ck_task_status`。
        CheckConstraint(f"kind IN {TASK_KINDS!r}", name="kind"),
        CheckConstraint(f"status IN {TASK_STATUSES!r}", name="status"),
    )
