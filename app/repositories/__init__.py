"""数据访问层出口〔阶段 3〕

只有 repository 允许写 SQL（AGENTS.md §3）。`node` 与 `api` 都通过 service 拿 repository，
构造时把 `AsyncSession` 注入进来——所以这里不建引擎，也不 import `app.utils.db`。
"""

from app.repositories.base import BaseRepository
from app.repositories.interview_repository import InterviewRepository
from app.repositories.job_repository import JobRepository
from app.repositories.question_repository import QuestionRepository
from app.repositories.resume_repository import ResumeRepository

__all__ = [
    "BaseRepository",
    "InterviewRepository",
    "JobRepository",
    "QuestionRepository",
    "ResumeRepository",
]
