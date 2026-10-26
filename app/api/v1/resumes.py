"""简历 CRUD：`POST /upload` · `GET /list` · `GET/PUT/DELETE /{id}`〔阶段 2，M6 后台化〕

上传链路（验收"上传简历 → 轮询 → 拿到 15 题"的前半段）：

1. 文件落盘 + `resume`/`task` 两行由服务层**当场提交**（`resume_service.accept_upload`）；
2. `run_resume_parsing` 挂进 `BackgroundTasks`，响应立刻带两个 id 返回（202）；
3. 客户端拿 `task_id` 去 `GET /tasks/{task_id}` 轮询进度。

这里注入的是会话工厂而不是 `SessionDep`：实测（FastAPI 0.143 + Starlette 1.8，
`scripts/probe_background_order.py`）后台协程在依赖收尾**之前**就跑起来了，
`get_db` 的提交赶不上——后台第一件事就会读到"简历行不存在"。ADR-0011 记的就是这条。
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, UploadFile

from app.dependencies import SessionDep, SessionMakerDep
from app.exceptions.base import NotFoundError
from app.schemas.resume import ResumeOut, ResumeUpdateRequest, UploadOut
from app.services import resume_service
from app.tasks.resume_parsing import run_resume_parsing

router = APIRouter(prefix="/resumes", tags=["resumes"])


@router.post("/upload", status_code=202, response_model=UploadOut)
async def upload_resume(
    background_tasks: BackgroundTasks, sessionmaker: SessionMakerDep, file: UploadFile
) -> UploadOut:
    # 简历 PDF/DOCX 都是"页级"小文件（M1 实测样本 ~200KB），整份进内存再落盘
    # 比流式写简单且不构成内存风险；真要收大附件时再换 `UploadFile` 的分块读。
    data = await file.read()
    resume, task = await resume_service.accept_upload(
        sessionmaker, file_name=file.filename or "", data=data
    )
    background_tasks.add_task(run_resume_parsing, task.id, resume.id)
    return UploadOut(resume_id=resume.id, task_id=task.id)


# `/list` 必须注册在 `/{resume_id}` 之前：Starlette 按注册顺序匹配，
# 动态段在前会让 "list" 被当成一个 resume_id 直接 404。
@router.get("/list", response_model=list[ResumeOut])
async def list_resumes(
    session: SessionDep, limit: int = 50, offset: int = 0
) -> list[ResumeOut]:
    rows = await resume_service.list_resumes(session, limit=limit, offset=offset)
    return [ResumeOut.model_validate(row) for row in rows]


@router.get("/{resume_id}", response_model=ResumeOut)
async def get_resume(resume_id: str, session: SessionDep) -> ResumeOut:
    resume = await resume_service.fetch_resume(session, resume_id)
    if resume is None:
        raise NotFoundError("简历不存在", details={"resume_id": resume_id})
    return ResumeOut.model_validate(resume)


@router.put("/{resume_id}", response_model=ResumeOut)
async def rename_resume(
    resume_id: str, body: ResumeUpdateRequest, session: SessionDep
) -> ResumeOut:
    """改展示名。抽取结果与原文不给改（`ResumeUpdateRequest` 里写着为什么）。"""
    resume = await resume_service.rename_resume(session, resume_id, body.name)
    if resume is None:
        raise NotFoundError("简历不存在", details={"resume_id": resume_id})
    return ResumeOut.model_validate(resume)


@router.delete("/{resume_id}", status_code=204)
async def delete_resume(
    resume_id: str, session: SessionDep, force: bool = False
) -> None:
    """删简历 + 删落盘原件。名下还有报告时返 409，`?force=true` 才连报告一起删。

    为什么不默认放行：`interview.resume_id` 是 `ON DELETE CASCADE`，这一删不可撤销。
    """
    deleted = await resume_service.delete_resume(session, resume_id, force=force)
    if not deleted:
        raise NotFoundError("简历不存在", details={"resume_id": resume_id})
