"""岗位 CRUD：`GET /jobs` · `POST /jobs` · `POST /jobs/upload` · `POST /jobs/inspect-image`
· `GET/PUT/DELETE /jobs/{id}`

出题链路要能选到 `job_id`，靠的是读端点；写端点是这次补的：JD 是**上传即定稿**的输入，
不需要解析阶段，所以写操作全部同步返回，没有 `task_id`、也不占 `BackgroundTasks`
（对比 `resumes.py` 的 202：那边后台要跑 LLM）。JSON/文件建岗一次模型调用都不花；
`/inspect-image` 要读图，花 1-2 次，但它只回草稿、不落库，等的是一份要人过目的表单。

`company` 允许空串——种子数据和招聘站上都有"只写岗位名不写公司"的 JD，而自然键
`title + company` 依然能去重。撞键返 409（`ConflictError`），并回带已存在行的 id，
让调用方能直接改成 PUT。

写端点用 `SessionDep`：这里没有后台任务，`get_db` 的请求事务够用（ADR-0011 的
`SessionMakerDep` 只在排了 `BackgroundTasks` 的端点上才是必需的）。唯一的例外是
`/inspect-image`，它不碰数据库，所以只注 `LLMDep`。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile

from app.dependencies import LLMDep, SessionDep
from app.exceptions.base import NotFoundError
from app.schemas.job import JobCreateRequest, JobDraftOut, JobOut, JobUpdateRequest
from app.services import job_service

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("", response_model=list[JobOut])
async def list_jobs(session: SessionDep, limit: int = 50, offset: int = 0) -> list[JobOut]:
    rows = await job_service.list_jobs(session, limit=limit, offset=offset)
    return [JobOut.model_validate(row) for row in rows]


@router.post("", status_code=201, response_model=JobOut)
async def create_job(body: JobCreateRequest, session: SessionDep) -> JobOut:
    """JSON 建岗：`required_skills` 留空时按词典从 `jd_text` 派生。"""
    job = await job_service.create_job(session, body)
    return JobOut.model_validate(job)


@router.post("/upload", status_code=201, response_model=JobOut)
async def upload_job(
    session: SessionDep,
    # `File(...)` / `Form(...)` 放进参数默认值会撞 ruff B008（与 `SessionDep` 同一条理由），
    # 所以声明挂在 Annotated 上，可选值用普通字面量默认。
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form()],
    company: Annotated[str, Form()] = "",
    min_years: Annotated[float, Form()] = 0.0,
) -> JobOut:
    """文件建岗：`.txt` / `.md` / `.pdf` / `.docx` 读成文本，其余流程与 `POST /jobs` 同一条。

    整份读进内存再解，理由与简历上传一样：JD 是页级小文件，几 KB 到几十 KB。
    """
    data = await file.read()
    job = await job_service.create_job_from_upload(
        session,
        file_name=file.filename or "",
        data=data,
        title=title,
        company=company,
        min_years=min_years,
    )
    return JobOut.model_validate(job)


@router.post("/inspect-image", response_model=JobDraftOut)
async def inspect_job_image(
    file: Annotated[UploadFile, File()], llm: LLMDep
) -> JobDraftOut:
    """图片建岗的第一步：读图 + 认字段，回一份**草稿**，不落库、不查库（所以没有 `session`）。

    为什么不一步到位：`title` 是 `job` 表自然键的一半，机器猜出来的岗位名静默入库，
    就等于用一条脏记录占住 `uq_job_title_company`。认不出的字段留空并写进
    `missing_required`，由页面提示人补上，补完走 `POST /jobs`——写路径、409、422 全是现成的。

    这是本模块唯一花模型调用的端点（1 次读图 + 至多 1 次补字段），但仍然同步返回：
    它的产出是一份要人过目的草稿，不是一个要轮询的任务。
    """
    data = await file.read()
    return await job_service.draft_job_from_image(file_name=file.filename or "", data=data, llm=llm)


@router.get("/{job_id}", response_model=JobOut)
async def get_job(job_id: str, session: SessionDep) -> JobOut:
    job = await job_service.get_job(session, job_id)
    if job is None:
        raise NotFoundError("岗位不存在", details={"job_id": job_id})
    return JobOut.model_validate(job)


@router.put("/{job_id}", response_model=JobOut)
async def update_job(job_id: str, body: JobUpdateRequest, session: SessionDep) -> JobOut:
    job = await job_service.update_job(session, job_id, body)
    if job is None:
        raise NotFoundError("岗位不存在", details={"job_id": job_id})
    return JobOut.model_validate(job)


@router.delete("/{job_id}", status_code=204)
async def delete_job(job_id: str, session: SessionDep) -> None:
    """删岗。历史报告留下——`interview.job_id` 是 `SET NULL`，标题公司有快照列兜着。"""
    deleted = await job_service.delete_job(session, job_id)
    if not deleted:
        raise NotFoundError("岗位不存在", details={"job_id": job_id})
