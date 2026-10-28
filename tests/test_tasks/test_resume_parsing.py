"""`run_resume_parsing`：解析结果回写 `resume` 行、进度落 `task` 行〔M6〕

parse 节点用假函数替身（真节点要读磁盘上的 PDF、要打 LLM——那是
`test_parse_node.py` 与端到端脚本的活）。这里验的是**任务边界**：
成功回写哪些列、失败把什么落进 `task.error`、异常绝不逃出协程。
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.exceptions.base import ParseError
from app.models.resume import Resume
from app.models.task import Task
from app.repositories.resume_repository import ResumeRepository
from app.services import task_service
from app.tasks import resume_parsing as resume_parsing_module
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


async def test_a_parsed_resume_whose_terminal_write_fails_is_logged(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """结果已经写进 `resume` 行了，收尾两笔落不进库：只记日志，不改口报失败〔T-009〕

    报 `failed` 会更糟——轮询方会以为没解析出来而重传一份，而库里那份是好的。
    """
    resume_id, task_id = await _seed(db_sessionmaker)

    async def broken_mark_completed(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("库被锁住了")

    monkeypatch.setattr(task_service, "mark_completed", broken_mark_completed)
    with caplog.at_level(logging.ERROR, logger="app.tasks.resume_parsing"):
        out = await run_resume_parsing(
            task_id, resume_id, sessionmaker=db_sessionmaker, node=_fake_node()
        )

    assert out["ok"] is True
    assert any(record.exc_info for record in caplog.records)
    assert (await _task_row(db_sessionmaker, task_id)).status == "running"
    async with db_sessionmaker() as session:
        resume = await ResumeRepository(session).get_by_id(resume_id)
    assert resume is not None and resume.name == "梁清生"


async def test_resume_deleted_mid_parse_reports_resume_gone(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """解析期间简历被删（`DELETE /resumes/{id}` 或 force 级联）：任务报失败，不报成功〔T-010〕

    报成功会让轮询方去读一行已经不存在的简历，比"解析失败"更难解释；`update` 返回 None
    就是"没有落点"的信号，这里把它翻成 `resume_gone`。
    """
    resume_id, task_id = await _seed(db_sessionmaker)
    parse = _fake_node()

    async def delete_then_parse(state: dict[str, Any]) -> dict[str, Any]:
        async with session_scope(db_sessionmaker) as session:
            await ResumeRepository(session).delete(resume_id)
        return await parse(state)

    out = await run_resume_parsing(
        task_id, resume_id, sessionmaker=db_sessionmaker, node=delete_then_parse
    )

    assert out == {"ok": False}
    row = await _task_row(db_sessionmaker, task_id)
    assert row.status == "failed"
    assert row.error["code"] == "resume_gone"
    assert row.error["details"] == {"resume_id": resume_id}
    # 进度里没有 `parse` 步：这一步确实没落成任何东西，记了反而和终态矛盾
    assert row.progress.get("steps", []) == []


async def test_missing_resume_row_does_not_crash_the_task(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """行从来不存在（API 事务回滚而后台已排上）：`update` 返 None，任务走失败分支而非炸掉。"""
    async with session_scope(db_sessionmaker) as session:
        task = await task_service.create_task(session, kind="resume_parsing", ref_id="ghost")
        task_id = str(task.id)

    out = await run_resume_parsing(
        task_id, "0" * 32, sessionmaker=db_sessionmaker, node=_fake_node()
    )
    assert out["ok"] is False
    row = await _task_row(db_sessionmaker, task_id)
    assert row.status == "failed" and row.error["code"] == "resume_gone"


async def test_client_built_by_the_task_is_closed_even_when_parse_fails(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """任务自建的真客户端在收尾被关闭，失败的运行也关〔T-011 / BACKLOG P1-6〕

    不关就是每一次后台运行攒一套 httpx 连接池。注入的 `llm` 归调用方管，所以这条只演
    "没注入客户端"的那条路。
    """
    from app.utils import llm_client as llm_client_module

    class _RecordingClient:
        def __init__(self, settings: Any = None) -> None:
            self.usage = llm_client_module.UsageTotals()
            self.settings = settings or llm_client_module.get_settings()
            self.closed = 0

        async def aclose(self) -> None:
            self.closed += 1

    created: list[_RecordingClient] = []

    def factory(settings: Any = None) -> _RecordingClient:
        client = _RecordingClient(settings)
        created.append(client)
        return client

    monkeypatch.setattr(resume_parsing_module, "LLMClient", factory)

    async def exploding_node(state: dict[str, Any]) -> dict[str, Any]:
        raise ParseError("扫描件读不出文本")

    resume_id, task_id = await _seed(db_sessionmaker)
    out = await run_resume_parsing(task_id, resume_id, sessionmaker=db_sessionmaker)
    assert out == {"ok": False}

    resume_id2, task_id2 = await _seed(db_sessionmaker)
    out2 = await run_resume_parsing(
        task_id2, resume_id2, sessionmaker=db_sessionmaker, node=exploding_node
    )
    assert out2 == {"ok": False}

    # 第一条没给 `node`：真 parse 节点自建客户端 → 关一次；第二条给了假节点 → 不建也不关
    assert [client.closed for client in created] == [1]
