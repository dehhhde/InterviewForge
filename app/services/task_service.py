"""任务进度的服务层：API 建行/读进度 + 后台任务写进度〔阶段 8 · M6〕

分层口径（AGENTS.md §3）：这里不写 SQL，全部走 `TaskRepository`；提交时机也归这里，
因为**后台任务的每一步都要立刻可见**——轮询方隔一秒读一次，看不到中途状态
就等于没有进度。所以写进度的四个函数各自开一个 `session_scope`（一次小事务），
而不是像 `persist_report` 那样攒一个跨表大事务：报告要"要么全有要么全无"，
进度只要"看到哪算哪"。

`AppError` 的失败详情只落 `{code, message, details}` 三个字段。`details` 由抛出方随手
构造，实测来路里有 `upload_dir`、`path`、`reason=str(exc)` 这类服务器绝对路径，而
`GET /tasks/{task_id}` 是无鉴权的读端点、全量透传，所以落库前先过一遍键白名单
（`ERROR_DETAIL_KEYS`）：白名单外的键只留键名，内容不出门。

失败终态本身也可能写不进库（连接、锁），那样任务就永远停在 `running`——
`mark_failed_logged` 是给任务边界用的那一层：写不进去也要把栈留在日志里，
否则连"有一个失败没被记下"这件事都没有记录。
停在 `running` 的行由 `reap_stale` 收尾（T-015）：超过阈值没有再写过进度，就翻成
`failed` + `task_stale`，让轮询方至少拿到一个结论，而不是永远等下去。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.exceptions.base import AppError
from app.models.base import utcnow_naive
from app.models.task import KIND_QUESTION_GENERATION, TASK_KINDS, Task
from app.repositories.interview_repository import InterviewRepository
from app.repositories.task_repository import TaskRepository
from app.utils.db import session_scope

logger = logging.getLogger("app.services.task")

#: `task.error.details` 允许出现在客户端的键。判据是"标识与计数"——
#: id 类给客户端定位业务行，计数类给成本/进度语义，两者都不携带路径或原文。
#: 新增节点要往外给字段，就在这里显式加键：白名单的意义在于默认拒绝，
#: 未来的节点不会因为是"新写的"就自动拿到外泄的许可。
ERROR_DETAIL_KEYS: frozenset[str] = frozenset(
    {
        "resume_id",
        "job_id",
        "interview_id",
        "task_id",
        "missing",
        "field",
        "interview_count",
        "used_tokens",
        "limit_tokens",
        "calls",
        "attempts",
        "page_count",
        "size",
        "characters",
        "stale_minutes",
    }
)
#: 被挡下的键以这个键名留档，客户端至少知道"有东西被收走了"。
DROPPED_KEYS_FIELD = "_dropped_keys"

#: 僵尸判据覆盖的状态（T-015）。理由写在 `reap_stale` 的 docstring 里，
#: 判据放成模块常量是为了让 dry-run 脚本和回收函数用同一个口径。
STALE_STATUSES: tuple[str, ...] = ("running",)
#: 回收写进 `task.error.code` 的值。它不由任何异常抛出，是从库里读出来的结论
STALE_ERROR_CODE = "task_stale"

#: 报告"完成但不可用"的两个成因〔T-309 / D-6〕。它们是**派生事实**，不是状态机取值：
#: 不新增 `interview.status` 枚举（那要动 CHECK 约束与迁移），只让读端点把"这一份卷子
#: 到底有没有内容"说清楚，客户端不必自己拿 `total_questions == 0` 猜。
DEGRADED_NO_QUESTIONS = "no_questions"
DEGRADED_REPORT_GONE = "report_gone"


@dataclass(frozen=True)
class StaleTask:
    """一行被判为僵尸的描述。脚本拿它打印，调用方拿它的 `task_id` 定位业务行。

    不直接返回 `Task` ORM 实例：`session_scope` 一退出对象就过期了，
    在事务外读它的列会抛 `MissingGreenlet`（ADR-0007 踩过的那类）。
    """

    task_id: str
    kind: str
    status: str
    ref_id: str | None
    #: 距上一次写进度过了多久。阈值是"多久算僵尸"，这个数是"僵尸躺了多久"
    stalled_minutes: int


def _detail_value_is_safe(value: object) -> bool:
    """白名单管键名，这一层管值：路径分隔符是"目录结构外泄"唯一的形状特征，
    而 dict 是 `plan`、`last_error` 这类大对象挤进 error 行的入口。"""
    if value is None or isinstance(value, bool | int | float):
        return True
    if isinstance(value, str):
        return "/" not in value and "\\" not in value
    if isinstance(value, (list, tuple)):
        return all(_detail_value_is_safe(item) for item in value)
    return False


def _safe_error_details(details: dict[str, Any]) -> dict[str, Any]:
    kept: dict[str, Any] = {
        key: value
        for key, value in details.items()
        if key in ERROR_DETAIL_KEYS and _detail_value_is_safe(value)
    }
    dropped = sorted(key for key in details if key not in kept)
    if dropped:
        kept[DROPPED_KEYS_FIELD] = dropped
    return kept


async def create_task(session: AsyncSession, *, kind: str, ref_id: str | None) -> Task:
    """API 在返回响应前插任务行（`pending`）。只 flush，提交归 `get_db`。"""
    if kind not in TASK_KINDS:
        raise ValueError(f"未知的任务类型：{kind!r}，可用：{TASK_KINDS}")
    return await TaskRepository(session).create(kind=kind, status="pending", ref_id=ref_id)


async def read_task(session: AsyncSession, task_id: str) -> Task | None:
    """轮询读取。找不到返回 None，404 是 API 层的语义。"""
    return await TaskRepository(session).get_by_id(task_id)


@dataclass(frozen=True)
class ReportAvailability:
    """`GET /tasks/{id}` 的派生字段：任务所属报告**到底能不能用**〔T-309 / D-6〕。

    为什么由服务端派生：`status=completed` 只说明后台跑完了，不说明有题——`review` 是纯批注
    （ADR-0015），0 题也会照常收尾。让每个客户端各写一遍"completed 且 total_questions == 0
    才该报警"就是把同一条判据抄成三份（控制台、脚本、将来的导出页），而且没人会记得同步它。
    """

    report_status: str | None = None
    question_count: int | None = None
    degraded: bool = False
    degraded_reason: str | None = None


async def report_availability(
    session: AsyncSession, task: Task
) -> ReportAvailability:
    """按任务行的 `kind` / `ref_id` 读报告头，得出派生字段。

    只对出题任务派生：简历解析任务的 `ref_id` 指向 `resume`，那里没有"几道题"这回事，
    硬套会让所有解析任务都带一个恒为 `None` 的 `degraded`，客户端反而要去猜它何时有意义。

    两种"完成了但不可用"：报告行还在却 `total_questions == 0`（`review` 一道题都没攒到），
    以及报告行被删了（删简历 `?force=true` 会连报告一起删，任务行却还在）。两者都不是
    `failed`——后台确实跑完了，说失败会让用户去重传，而重传只会得到同一批语料。
    """
    if task.kind != KIND_QUESTION_GENERATION or not task.ref_id:
        return ReportAvailability()
    head = await InterviewRepository(session).get_by_id(task.ref_id)
    if head is None:
        return ReportAvailability(
            degraded=task.status == "completed",
            degraded_reason=DEGRADED_REPORT_GONE if task.status == "completed" else None,
        )
    degraded = task.status == "completed" and head.total_questions == 0
    return ReportAvailability(
        report_status=head.status,
        question_count=head.total_questions,
        degraded=degraded,
        degraded_reason=DEGRADED_NO_QUESTIONS if degraded else None,
    )


async def mark_running(sessionmaker: async_sessionmaker[AsyncSession], task_id: str) -> None:
    """后台协程真正开始执行时翻 `pending -> running`。

    行不存在时静默跳过（`finish` 同理）：任务行是 API 响应事务里提交的，API 回滚了
    而后台已经排上执行是可能发生的（同一请求内 background 在响应后才跑，若响应事务
    提交失败）。给这种竞态抛异常没有意义，进度丢了就丢了，业务行本来也不存在。
    """
    async with session_scope(sessionmaker) as session:
        await TaskRepository(session).set_status(task_id, "running")


async def record_step(
    sessionmaker: async_sessionmaker[AsyncSession],
    task_id: str,
    node: str,
    *,
    ms: float | None = None,
    ok: bool = True,
    detail: str = "",
) -> None:
    async with session_scope(sessionmaker) as session:
        repo = TaskRepository(session)
        step: dict[str, Any] = {"node": node}
        if ms is not None:
            step["ms"] = ms
        if not ok:
            # 成功步不带 `ok` 键（历史行就是这个形状，读侧 `step.get("ok")` 真值即通过）。
            # 失败才显式留 `ok=False` + 错误码，否则"停在哪个节点中止的"在进度里读不出来。
            step["ok"] = False
            step["detail"] = detail
        await repo.append_step(task_id, step)


async def mark_completed(
    sessionmaker: async_sessionmaker[AsyncSession],
    task_id: str,
    result: dict[str, Any],
) -> None:
    async with session_scope(sessionmaker) as session:
        await TaskRepository(session).finish(task_id, "completed", result=result)


async def mark_failed(
    sessionmaker: async_sessionmaker[AsyncSession],
    task_id: str,
    exc: BaseException,
) -> None:
    if isinstance(exc, AppError):
        error: dict[str, Any] = {
            "code": exc.code,
            "message": exc.message,
            "details": _safe_error_details(exc.details),
        }
    else:
        error = {
            "code": "internal_error",
            "message": f"{type(exc).__name__}: {exc}"[:500],
            "details": {},
        }
    async with session_scope(sessionmaker) as session:
        await TaskRepository(session).finish(task_id, "failed", error=error)


def _stale_cutoff(minutes: int | None) -> int:
    """阈值：显给的 `minutes` 优先，否则取 `configs/*.yaml` 的 `task.stale_after_minutes`。

    在函数体里取 settings 而不是模块常量：`get_settings()` 是 lru_cache 单例，
    测试要用 monkeypatch 换值，常量会把换不动的那份烧进 import 里。
    """
    return minutes if minutes is not None else get_settings().task.stale_after_minutes


async def mark_failed_logged(
    sessionmaker: async_sessionmaker[AsyncSession],
    task_id: str,
    exc: BaseException,
) -> None:
    """给任务边界用的 `mark_failed`：连"报失败"这件事都失败时，至少日志里有栈。

    为什么不重抛：调用它的那一层本来就是"吞掉一切并落库"，落库也没落成时再往上抛
    只会被 Starlette 记一条没有上下文的日志。僵尸行的回收是另一条路（`reap_stale`）。
    """
    try:
        await mark_failed(sessionmaker, task_id, exc)
    except Exception:
        logger.exception("任务 %s 的失败终态写库没成功，状态会停在 running", task_id)


async def list_stale(
    session: AsyncSession,
    *,
    minutes: int | None = None,
    statuses: Sequence[str] = STALE_STATUSES,
) -> Sequence[Task]:
    """列出"停在半路、很久没动过"的任务行。只读，不改状态。"""
    cutoff = utcnow_naive() - timedelta(minutes=_stale_cutoff(minutes))
    return await TaskRepository(session).list_stale(before=cutoff, statuses=statuses)


async def reap_stale(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    minutes: int | None = None,
    apply: bool = True,
    statuses: Sequence[str] = STALE_STATUSES,
) -> list[StaleTask]:
    """僵尸任务回收〔T-015〕。返回被认定僵尸的行；`apply=False` 只认定、不写库。

    为什么需要它：`BackgroundTasks` 跑在本进程，进程一重启（或 `mark_failed_logged`
    连失败终态都没写进去），那一行就永远停在 `running`，轮询方会一直等一个不会来的终局。
    这里给它一个有依据的结论：`task_stale`，并把停滞时长留在 `details` 里。

    为什么 `pending` 不在默认状态里：一行 `pending` 也可能是"响应已提交、后台还没排上"
    的正常瞬间，误判的代价是把好端端跑着的工作说成失败；而 `running` 一旦长时间停止更新，
    只剩"进程没了"这一种解释。
    """
    async with session_scope(sessionmaker) as session:
        found = await list_stale(session, minutes=minutes, statuses=statuses)
        if not found:
            return []
        threshold = _stale_cutoff(minutes)
        repo = TaskRepository(session)
        reaped: list[StaleTask] = []
        for task in found:
            stalled = int((utcnow_naive() - task.updated_at).total_seconds() // 60)
            reaped.append(
                StaleTask(
                    task_id=task.id,
                    kind=task.kind,
                    status=task.status,
                    ref_id=task.ref_id,
                    stalled_minutes=stalled,
                )
            )
            if apply:
                await repo.finish(
                    task.id,
                    "failed",
                    error={
                        "code": STALE_ERROR_CODE,
                        "message": (
                            f"任务停在 {task.status} 已 {stalled} 分钟没有进展，"
                            f"按 {threshold} 分钟的阈值判为进程中断留下的僵尸行"
                        ),
                        "details": {"task_id": task.id, "stale_minutes": stalled},
                    },
                )
        return reaped
