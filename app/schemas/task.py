"""任务进度 DTO〔阶段 8 · M6〕

`GET /tasks/{task_id}` 的响应形状。`progress` / `result` / `error` 是任务自己写的
JSON——API 只透传不解释，客户端按 `status` 分支，绝不在 DTO 里给"下一步该干嘛"
埋逻辑（那是轮询方的事）。
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
