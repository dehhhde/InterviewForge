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
    #: 授权"评审点过名的档重出一遍"〔T-404 / ADR-0021〕。默认关：一次成卷是既有契约，
    #: 而开了它最多多烧一轮扇出——花的是钱，所以必须由调用方明说，不能服务端替它决定。
    auto_fix: bool = False


class InterviewStartOut(BaseModel):
    """开跑回执：轮询看 `task_id`，取结果看 `interview_id`。"""

    interview_id: str
    task_id: str
    status: str = "running"


class ReportQuestion(QuestionSchema):
    """报告里的一道题 = 产出契约 + 题号。"""

    order_index: int = 0


class InterviewHeadFields(BaseModel):
    """报告头的列：`interview` 表上有的都在这里，列表行与详情**共用**〔T-301 / OPT-6.1〕。

    字段名只声明一次：详情模型加的是"题目行"这一份增量，而不是把十三个列再抄一遍。
    """

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
    created_at: datetime
    updated_at: datetime


class InterviewListItem(InterviewHeadFields):
    """`GET /interviews/list` 的行：报告头 + 出题计划 + 批注，**没有题目行**〔T-301〕。

    以前列表复用 `InterviewOut` 并把 `questions` 留空数组，于是同一个空数组同时表示
    "这份卷子确实没题"（`aborted` 的真实状态）和"列表不下发正文"——读的人分不清，
    而分不清的字段迟早被人当真相用（`ReportsView` 导出前必须先取一次详情就是为它打的补丁）。
    计划与批注留在列表行里是刻意的：报告中心的展开行直接渲染它们，不再逐行请求详情。
    """


class InterviewOut(InterviewHeadFields):
    """报告详情：报告头 + 该批题目。"""

    questions: list[ReportQuestion] = []
