"""v1 路由聚合〔阶段 2 · §5.1，M6 落地〕

`prefix="/api/v1"` 在这里统一挂：各端点模块只写自己那段前缀（`/resumes`、`/tasks`…），
将来 v2 并存时只动本文件。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import health, interviews, jobs, resumes, tasks

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(resumes.router)
api_router.include_router(interviews.router)
api_router.include_router(tasks.router)
api_router.include_router(jobs.router)
