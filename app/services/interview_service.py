"""面试报告服务：开跑一次生成 + 读回报告〔阶段 7 建壳，M6 落地〕

分层位置：API 不 import models、不碰 repository，这里的函数就是两端之间唯一的路。
`start_generation` 只建 `interview(running)` + `task(pending)` 两行并交回 `jd_text`——
图由 API 挂给 `BackgroundTasks`（`app/tasks/question_generation.py`），题目落库仍只在
`review` 收尾出口，服务层不提前写任何题目（AGENTS.md §4.6 的幂等前提）。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.exceptions.base import ConflictError, NotFoundError
from app.models.interview import Interview
from app.models.question import Question
from app.models.task import KIND_QUESTION_GENERATION, Task
from app.repositories.base import keyword_filter
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

    三个业务判据在这里而不是端点里：两个 404 是"能不能开跑"，同一个 `(resume_id, job_id)`
    已有 `running` 报告则抛 `ConflictError`（409，details 带既有 `interview_id`）〔T-209 / D-5〕。
    判据选 `(resume_id, job_id)` 而不是只看简历：同一份简历换岗位重跑是主流程，
    而同一对**同时**跑两份只会在扇出阶段重复烧一遍 token，先到的那份已经有结论可等了。
    这是**先查后插**，不是唯一约束：两个请求如果恰好同时通过检查，仍可能各建一行
    （SQLite 单写者只保证写入串行，不保证检查与插入之间没人插队）。挡的是误触双击，
    不是恶意并发——后者要鉴权与配额，归 D-7 的讨论。

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

        interviews = InterviewRepository(session)
        running = await interviews.get_running_for(resume.id, job.id)
        if running is not None:
            raise ConflictError(
                "这份简历对这个岗位已有一份报告正在生成，等它跑完再看；"
                "确实要重跑，等它结束或先删掉那份报告",
                details={
                    "interview_id": running.id,
                    "resume_id": resume.id,
                    "job_id": job.id,
                },
            )

        head = await interviews.create(
            resume_id=resume.id,
            job_id=job.id,
            plan={},
            status="running",
            # 线程 id 在**开跑前**就定下来：它既是 checkpoint 的归属，也是"这份报告的图终态
            # 在哪个线程"的唯一寻址方式（导出与排障据此读单线程，不再扫全库）。
            # 拿 `interview_id` 当线程 id 看着更省，但那是错的：同一 id 第二次 `astream`
            # 会**续跑**上一个线程（把上次失败留下的 pending writes 一起捡回来），
            # 而随机 uuid 保证每次开跑都是干净线程。所以这里显式生成、显式落库。
            thread_id=uuid.uuid4().hex,
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
    session: AsyncSession, *, limit: int = 50, offset: int = 0, q: str = ""
) -> tuple[Sequence[Interview], int]:
    """一页报告头 + **同一条件下**的总行数〔T-303〕。

    搜索列取报告头自己的四列快照（`resume_name` / `resume_file_name` / `job_title` /
    `job_company`）加主键 `id`，不回 join 源表：ADR-0013 定的就是"这份卷子是给谁、按哪个岗位出的"
    属于报告的事实。join 回去搜还会撞上岗位已删（`job_id` 是 `SET NULL`）——那时候
    搜索会把明明存在的报告漏掉。`id` 参与匹配是为了保住旧前端本地过滤就有的能力：
    贴一段 report id 就能定位那一行，`LIKE '%…%'` 对 uuid hex 前缀同样成立。
    """
    repository = InterviewRepository(session)
    filters = keyword_filter(
        Interview, q, ("id", "resume_name", "resume_file_name", "job_title", "job_company")
    )
    rows = await repository.list(limit=limit, offset=offset, filters=filters)
    return rows, await repository.count(filters=filters)
