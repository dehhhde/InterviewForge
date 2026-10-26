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

from time import perf_counter
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.nodes.parse_node import make_parse_node
from app.agents.workflow import PROMPT_VERSION_KEY, Node
from app.config import Settings, get_settings
from app.dependencies import get_sessionmaker
from app.repositories.resume_repository import ResumeRepository
from app.services import task_service
from app.utils.db import session_scope
from app.utils.llm_client import LLMClient


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

    parser = node or make_parse_node(llm=llm or LLMClient(settings), settings=settings)
    try:
        started = perf_counter()
        update = dict(await parser({"resume_id": resume_id}))
    except Exception as exc:  # 任务边界就是要兜住一切，落库后不再上抛
        await task_service.mark_failed(sessions, task_id, exc)
        return {"ok": False}

    notes = [str(note) for note in update.get("errors") or []]
    parsed = update.get("parsed") or {}
    try:
        async with session_scope(sessions) as session:
            await ResumeRepository(session).update(
                resume_id,
                raw_text=str(update.get("raw_text") or ""),
                parsed=parsed,
                name=str(parsed.get("name") or ""),
            )
    except Exception as exc:  # 同上：写库失败也要给轮询方一个 `failed`
        await task_service.mark_failed(sessions, task_id, exc)
        return {"ok": False}

    await task_service.record_step(
        sessions, task_id, "parse", ms=round((perf_counter() - started) * 1000, 2)
    )
    result: dict[str, Any] = {
        "resume_id": resume_id,
        "name": str(parsed.get("name") or ""),
        "note_count": len(notes),
    }
    prompt_version = str(update.pop(PROMPT_VERSION_KEY, ""))
    if prompt_version:
        result["prompt_version"] = prompt_version
    await task_service.mark_completed(sessions, task_id, result)
    return {"ok": True, **result}
