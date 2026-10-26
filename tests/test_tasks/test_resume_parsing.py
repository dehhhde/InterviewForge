"""`run_resume_parsing`：解析结果回写 `resume` 行、进度落 `task` 行〔M6〕

parse 节点用假函数替身（真节点要读磁盘上的 PDF、要打 LLM——那是
`test_parse_node.py` 与端到端脚本的活）。这里验的是**任务边界**：
成功回写哪些列、失败把什么落进 `task.error`、异常绝不逃出协程。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.exceptions.base import ParseError
from app.models.resume import Resume
from app.models.task import Task
from app.repositories.resume_repository import ResumeRepository
from app.services import task_service
from app.tasks.resume_parsing import run_resume_parsing
from app.utils.db import session_scope

_PARSED = {
    "name": "梁清生",
    "years_experience": 5.0,
    "skills": [{"name": "Redis", "evidence": "使用 Redis 实现滑动窗口限流"}],
}


async def _seed(db_sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[str, str]:
    async with session_scope(db_sessionmaker) as session:
        resume = await ResumeRepository(session).create(
            stored_path="abc.pdf", file_name="附件简历.pdf"
        )
        task = await task_service.create_task(
            session, kind="resume_parsing", ref_id=resume.id
        )
        return str(resume.id), str(task.id)


async def _task_row(
    db_sessionmaker: async_sessionmaker[AsyncSession], task_id: str
) -> Task:
    async with db_sessionmaker() as session:
        row = await task_service.read_task(session, task_id)
    assert row is not None
    return row


def _fake_node(**overrides: Any):
    async def node(state: dict[str, Any]) -> dict[str, Any]:
        update: dict[str, Any] = {
            "raw_text": "项目经历 使用 Redis 实现滑动窗口限流",
            "parsed": dict(_PARSED),
            "errors": ["parse: 某条流水账"],
            "prompt_version": "ner@v1",
        }
        update.update(overrides)
        return update

    return node


async def test_success_writes_resume_row_and_completes_task(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    resume_id, task_id = await _seed(db_sessionmaker)

    out = await run_resume_parsing(
        task_id, resume_id, sessionmaker=db_sessionmaker, node=_fake_node()
    )

    assert out["ok"] is True and out["name"] == "梁清生"
    async with db_sessionmaker() as session:
        resume: Resume | None = await ResumeRepository(session).get_by_id(resume_id)
    assert resume is not None
    assert resume.name == "梁清生" and resume.raw_text.startswith("项目经历")
    assert resume.parsed["years_experience"] == 5.0

    row = await _task_row(db_sessionmaker, task_id)
    assert row.status == "completed"
    assert row.result["prompt_version"] == "ner@v1"
    assert [step["node"] for step in row.progress["steps"]] == ["parse"]


async def test_parse_failure_lands_in_task_error_not_in_the_lap(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    resume_id, task_id = await _seed(db_sessionmaker)

    async def exploding_node(state: dict[str, Any]) -> dict[str, Any]:
        raise ParseError("扫描件读不出文本", details={"file": "abc.pdf"})

    out = await run_resume_parsing(
        task_id, resume_id, sessionmaker=db_sessionmaker, node=exploding_node
    )

    assert out == {"ok": False}
    row = await _task_row(db_sessionmaker, task_id)
    assert row.status == "failed"
    assert row.error["code"] == "parse_failed"
    # 失败不回写半成品：`resume` 行保持建行时的空值
    async with db_sessionmaker() as session:
        resume = await ResumeRepository(session).get_by_id(resume_id)
    assert resume is not None and resume.raw_text == "" and resume.parsed == {}


async def test_unexpected_exception_is_recorded_as_internal_error(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    resume_id, task_id = await _seed(db_sessionmaker)

    async def weird_node(state: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("解析器内部炸了")

    await run_resume_parsing(
        task_id, resume_id, sessionmaker=db_sessionmaker, node=weird_node
    )
    row = await _task_row(db_sessionmaker, task_id)
    assert row.status == "failed" and row.error["code"] == "internal_error"


async def test_missing_resume_row_does_not_crash_the_task(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """`update` 找不到行返回 None（repository 约定），任务照样走完成分支。"""
    async with session_scope(db_sessionmaker) as session:
        task = await task_service.create_task(session, kind="resume_parsing", ref_id="ghost")
        task_id = str(task.id)

    out = await run_resume_parsing(
        task_id, "0" * 32, sessionmaker=db_sessionmaker, node=_fake_node()
    )
    assert out["ok"] is True
    assert (await _task_row(db_sessionmaker, task_id)).status == "completed"
