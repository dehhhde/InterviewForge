"""POST /generate · GET /{id} · GET /list〔阶段 2 建壳，M6 接 BackgroundTasks〕

与 `resumes.py` 的上传同构：服务层建 `interview(running)` + `task(pending)` 并**当场提交**，
整张 LangGraph 交给 `BackgroundTasks` 后台跑，响应立刻带 `interview_id` + `task_id`。
注入会话工厂而不是请求会话的理由见 `resumes.py` 顶部（ADR-0011）。
出题进度（每个 super-step 一条）写进 `task.progress`，题目落库只在 `review` 收尾——
`GET /{id}` 在跑完前读到的就是 `status=running` 加空题目，这正是轮询方要的语义。
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter, BackgroundTasks

from app.dependencies import SessionDep, SessionMakerDep
from app.exceptions.base import NotFoundError
from app.models.interview import Interview
from app.models.question import Question
from app.schemas.interview import (
    GenerateRequest,
    InterviewOut,
    InterviewStartOut,
    ReportQuestion,
)
from app.services import interview_service
from app.tasks.question_generation import run_question_generation

router = APIRouter(prefix="/interviews", tags=["interviews"])

#: 报告 DTO 里题目行的字段来自 `question` 表的这些列，`batch` 等工序列不外泄。
_QUESTION_FIELDS = (
    "order_index",
    "stem",
    "difficulty",
    "source",
    "evidence",
    "checkpoints",
    "reference_answer",
    "rubric",
    "follow_ups",
)


@router.post("/generate", status_code=202, response_model=InterviewStartOut)
async def generate_questions(
    background_tasks: BackgroundTasks, sessionmaker: SessionMakerDep, body: GenerateRequest
) -> InterviewStartOut:
    head, task, jd_text = await interview_service.start_generation(
        sessionmaker, resume_id=body.resume_id, job_id=body.job_id
    )
    background_tasks.add_task(
        run_question_generation, task.id, body.resume_id, head.id, jd_text
    )
    return InterviewStartOut(interview_id=head.id, task_id=task.id)


# `/list` 的注册顺序约束见 `resumes.py` 的注释。
@router.get("/list", response_model=list[InterviewOut])
async def list_interviews(
    session: SessionDep, limit: int = 50, offset: int = 0
) -> list[InterviewOut]:
    rows = await interview_service.list_reports(session, limit=limit, offset=offset)
    return [_to_out(head, []) for head in rows]


@router.get("/{interview_id}", response_model=InterviewOut)
async def get_interview(interview_id: str, session: SessionDep) -> InterviewOut:
    report = await interview_service.fetch_report(session, interview_id)
    if report is None:
        raise NotFoundError("报告不存在", details={"interview_id": interview_id})
    head, questions = report
    return _to_out(head, questions)


def _to_out(head: Interview, questions: Sequence[Question]) -> InterviewOut:
    return InterviewOut(
        id=head.id,
        resume_id=head.resume_id,
        job_id=head.job_id,
        resume_name=head.resume_name,
        resume_file_name=head.resume_file_name,
        job_title=head.job_title,
        job_company=head.job_company,
        status=head.status,
        plan=dict(head.plan or {}),
        total_questions=int(head.total_questions),
        review_issues=[dict(issue) for issue in head.review_issues or []],
        questions=[
            ReportQuestion(**{field: getattr(row, field) for field in _QUESTION_FIELDS})
            for row in questions
        ],
        created_at=head.created_at,
        updated_at=head.updated_at,
    )
