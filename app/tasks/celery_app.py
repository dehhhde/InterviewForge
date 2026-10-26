"""Celery 应用：队列路由 / acks_late / 超时〔阶段 8 · §11.2〕

本模块已被 `FastAPI BackgroundTasks` 取代，不实现（ADR-0011，2026-10-20）：
单机部署、任务量以"一次面试一轮图"计，持久队列与跨进程 worker 都是负资产——
真需要时再加 Celery/RQ，而任务边界（只传 id、进度写 `task` 表）已经按那个形状留好了。
真正的任务实现见 `app/tasks/resume_parsing.py` 与 `question_generation.py`。
"""
