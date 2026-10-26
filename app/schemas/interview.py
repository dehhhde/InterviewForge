"""面试报告 DTO〔阶段 7 · §10.4.7，M6 落地读写两端〕

`POST /interviews/generate` 的入参与 `GET /interviews/{id}` 的产物。
报告里的题目直接复用 `QuestionSchema`（生成时的产出契约与读取时的展示契约同形，
`review` 落库前也已经按它验过一遍）；`order_index` 是报告题号，来自 `order_questions`
排序后的落库位置，与完成顺序无关（ADR-0010）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.question import QuestionSchema


class GenerateRequest(BaseModel):
    """对某份已解析的简历、按某个岗位的 JD 出题。"""

    resume_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)


class InterviewStartOut(BaseModel):
    """开跑回执：轮询看 `task_id`，取结果看 `interview_id`。"""

    interview_id: str
    task_id: str
    status: str = "running"


class ReportQuestion(QuestionSchema):
    """报告里的一道题 = 产出契约 + 题号。"""

    order_index: int = 0


class InterviewOut(BaseModel):
    id: str
    resume_id: str
    job_id: str | None = None
    #: 生成那一刻的简历/岗位快照（`interview` 表列，不是回 join 出来的）。
    #: 报告列表靠它一行写出"哪份简历 × 哪个岗位"，岗位被删（`SET NULL`）后依然读得到。
    resume_name: str = ""
    resume_file_name: str = ""
    job_title: str = ""
    job_company: str = ""
    status: str
    plan: dict[str, Any] = {}
    total_questions: int = 0
    review_issues: list[dict[str, Any]] = []
    questions: list[ReportQuestion] = []
    created_at: datetime
    updated_at: datetime
