"""面试报告服务：开跑一次生成 + 读回报告〔阶段 7 建壳，M6 落地〕

分层位置：API 不 import models、不碰 repository，这里的函数就是两端之间唯一的路。
`start_generation` 只建 `interview(running)` + `task(pending)` 两行并交回 `jd_text`——
图由 API 挂给 `BackgroundTasks`（`app/tasks/question_generation.py`），题目落库仍只在
`review` 收尾出口，服务层不提前写任何题目（AGENTS.md §4.6 的幂等前提）。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.exceptions.base import NotFoundError
from app.models.interview import Interview
from app.models.question import Question
from app.models.task import KIND_QUESTION_GENERATION, Task
from app.repositories.interview_repository import InterviewRepository
from app.repositories.job_repository import JobRepository
from app.repositories.question_repository import QuestionRepository
from app.repositories.resume_repository import ResumeRepository
from app.services import task_service
from app.utils.db import session_scope


async def start_generation(
    sessionmaker: async_sessionmaker[AsyncSession], *, resume_id: str, job_id: str
) -> tuple[Interview, Task, str]:
    """建报告头 + 任务行并**当场提交**，返回 `(interview, task, jd_text)`。

    两个 404 判在这里而不是端点里：`resume_id` 与 `job_id` 同时存在才构成"能开跑"，
    这是业务规则；端点只负责把 `NotFoundError` 翻译成 HTTP 状态。

    自己开事务的理由与 `resume_service.accept_upload` 相同（ADR-0011）：整张图挂在
    `BackgroundTasks` 上，而它在 `get_db` 收尾之前就开始跑，读不到未提交的报告头。
    """
    async with session_scope(sessionmaker) as session:
        resume = await ResumeRepository(session).get_by_id(resume_id)
        if resume is None:
            raise NotFoundError("简历不存在，无法出题", details={"resume_id": resume_id})
        job = await JobRepository(session).get_by_id(job_id)
        if job is None:
            raise NotFoundError("岗位不存在，无法出题", details={"job_id": job_id})

        head = await InterviewRepository(session).create(
            resume_id=resume.id,
            job_id=job.id,
            plan={},
            status="running",
            # 快照：报告生成之后源表可能改名、岗位可能下线，这四列是"当时给谁、按哪个岗位
            # 出的卷子"的唯一真相。空名回落到文件名，与迁移的回填口径一致。
            resume_name=resume.name or resume.file_name,
            resume_file_name=resume.file_name,
            job_title=job.title,
            job_company=job.company,
        )
        task = await task_service.create_task(
            session, kind=KIND_QUESTION_GENERATION, ref_id=head.id
        )
        jd_text = str(job.jd_text)
    return head, task, jd_text


async def fetch_report(
    session: AsyncSession, interview_id: str
) -> tuple[Interview, Sequence[Question]] | None:
    """读一份报告：报告头 + 最新批次的题目（`running` 中途读到时题表还是空的，属正常）。

    找不到返回 `None`——404 的判断留给端点（repository 的约定一路传上来）。
    """
    head = await InterviewRepository(session).get_by_id(interview_id)
    if head is None:
        return None
    rows = await QuestionRepository(session).list_for_interview(interview_id)
    return head, rows


async def list_reports(
    session: AsyncSession, *, limit: int = 50, offset: int = 0
) -> Sequence[Interview]:
    return await InterviewRepository(session).list(limit=limit, offset=offset)
