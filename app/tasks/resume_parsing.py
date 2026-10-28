"""简历解析异步任务，只传 ID 不传对象〔阶段 8 · §11.3〕

`BackgroundTasks` 替掉 Celery（ADR-0011）后，"任务"就是 FastAPI 响应之后在本进程跑的
一个协程。契约由 API 保证：调用前 `resume` 行与 `task` 行都已提交，这里只认两个 id，
文件路径按 `<upload_dir>/<resume_id><后缀>` 的命名约定由 parse 节点自己定位。

解析逻辑不在这里重写：`make_parse_node` 本就是"给它 state、它还你通道增量"的纯函数，
脱离图也能跑（`traced()` 只是观测包装，不是功能前置）。这样上传链路与图里的 parse
用的是同一份抽取代码，改抽取器不会有一边忘了改。

失败一律吞在任务边界内并落进 `task.error`：后台协程抛出去只会被 Starlette 记一条
日志，没人接；轮询方拿得到 `failed` 状态和错误码，才是可恢复的信息。
"""

from __future__ import annotations

import logging
from time import perf_counter
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.nodes.parse_node import make_parse_node
from app.agents.workflow import PROMPT_VERSION_KEY, Node
from app.config import Settings, get_settings
from app.dependencies import get_sessionmaker
from app.exceptions.base import AppError
from app.repositories.resume_repository import ResumeRepository
from app.services import task_service
from app.utils.db import session_scope
from app.utils.llm_client import LLMClient

logger = logging.getLogger("app.tasks.resume_parsing")


async def run_resume_parsing(
    task_id: str,
    resume_id: str,
    *,
    llm: LLMClient | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    node: Node | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """解析一份已上传的简历，把 `raw_text` / `parsed` / `name` 回写 `resume` 行。

    `node` 是测试注入口（假 parse 节点），与 `llm` 二选一：给了 `llm` 就用真 parse 节点。
    返回落库的增量摘要，方便脚本与测试直接看结果；API 的后台调用不关心返回值。
    """
    settings = settings or get_settings()
    sessions = sessionmaker or get_sessionmaker()
    await task_service.mark_running(sessions, task_id)

    if node is not None:
        parser = node
    else:
        client = llm or LLMClient(settings)
        parser = make_parse_node(llm=client, settings=settings)
    try:
        started = perf_counter()
        update = dict(await parser({"resume_id": resume_id}))
    except Exception as exc:  # 任务边界就是要兜住一切，落库后不再上抛
        await task_service.mark_failed_logged(sessions, task_id, exc)
        return {"ok": False}
    finally:
        # 后台任务自建的真客户端没人关就会攒连接池（T-011）；注入的替身归调用方管。
        if node is None and llm is None:
            await client.aclose()

    notes = [str(note) for note in update.get("errors") or []]
    parsed = update.get("parsed") or {}
    try:
        async with session_scope(sessions) as session:
            row = await ResumeRepository(session).update(
                resume_id,
                raw_text=str(update.get("raw_text") or ""),
                parsed=parsed,
                name=str(parsed.get("name") or ""),
            )
    except Exception as exc:  # 同上：写库失败也要给轮询方一个 `failed`
        await task_service.mark_failed_logged(sessions, task_id, exc)
        return {"ok": False}

    if row is None:
        # 解析期间简历被删了（`DELETE /resumes/{id}` 或 `force` 级联）。
        # 报成功等于让轮询方去读一行已经不存在的简历，比报失败更难解释。
        await task_service.mark_failed_logged(
            sessions,
            task_id,
            AppError(
                "简历在解析过程中被删除，解析结果没有落点",
                code="resume_gone",
                status_code=404,
                details={"resume_id": resume_id},
            ),
        )
        return {"ok": False}

    result: dict[str, Any] = {
        "resume_id": resume_id,
        "name": str(parsed.get("name") or ""),
        "note_count": len(notes),
    }
    prompt_version = str(update.pop(PROMPT_VERSION_KEY, ""))
    await _record_completed(sessions, task_id, started, result, prompt_version)
    return {"ok": True, **result}


async def _record_completed(
    sessions: async_sessionmaker[AsyncSession],
    task_id: str,
    started: float,
    result: dict[str, Any],
    prompt_version: str,
) -> None:
    """成功终态与 `parse` 进度步落库：写不进去只记日志，不改口报失败。

    结果已经在那一刻写进 `resume` 行了，收尾这两笔只是给轮询方看的；把它们裸在 `try`
    外面会让 DB 一抖就冒出后台协程，任务停在 `running` 而业务行是好的（BACKLOG P1-4）。
    """
    try:
        await task_service.record_step(
            sessions, task_id, "parse", ms=round((perf_counter() - started) * 1000, 2)
        )
        if prompt_version:
            result["prompt_version"] = prompt_version
        await task_service.mark_completed(sessions, task_id, result)
    except Exception:
        logger.exception(
            "简历 %s 解析完了但终态没写进去，轮询方会一直看到 running", result["resume_id"]
        )
