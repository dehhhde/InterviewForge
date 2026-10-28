"""任务进度 DTO〔阶段 8 · M6〕

`GET /tasks/{task_id}` 的响应形状。`progress` / `result` / `error` 是任务自己写的
JSON——API 只透传不解释，客户端按 `status` 分支，绝不在 DTO 里给"下一步该干嘛"
埋逻辑（那是轮询方的事）。

`report_status` / `question_count` / `degraded` / `degraded_reason` 是**服务端派生**的
事实〔T-309 / D-6〕：`status=completed` 只说明后台跑完了，`completed` 但 0 题的报告
（ADR-0015 之后评审不拦门，0 题也会收尾）要靠这几个字段才看得见，否则客户端只能自己
拿 `total_questions == 0` 各写一遍判据。`degraded` 对非出题任务恒为 `false`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: str
    status: str
    ref_id: str | None = None
    progress: dict[str, Any] = {}
    result: dict[str, Any] = {}
    error: dict[str, Any] = {}
    created_at: datetime
    updated_at: datetime
    #: 任务所属报告的生命周期状态（`running` / `completed` / `aborted`）；非出题任务为空
    report_status: str | None = None
    #: 报告头记录的题目数（`interview.total_questions`）；非出题任务为空
    question_count: int | None = None
    #: 后台跑完了但这份卷子不可用（0 题，或报告行已被删）
    degraded: bool = False
    #: 成因码：`no_questions` / `report_gone`；`degraded` 为假时为空
    degraded_reason: str | None = None
