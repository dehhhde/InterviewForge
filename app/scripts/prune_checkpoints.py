"""checkpoint 库清理 CLI〔T-311 / OPT-8.4 · BACKLOG P2-10〕

跑法（`PYTHONPATH=.` 不能省，`app.scripts` 不是已安装包）：

    PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/Scripts/python.exe -X utf8 \
        -m app.scripts.prune_checkpoints [--apply] [--keep-days N] [--include-orphans] [--vacuum]

`AsyncSqliteSaver` 只写不删，所以 `data/checkpoints.db` 从蓝图的 17.9MB 涨到实测
**21,770,240 字节 / 13 个线程**（2026-10-28，本机开发库）。每跑一次端到端就多一批行：
一次运行留 14-20 个 checkpoint，每个 checkpoint 里都存着**完整的 `AgentState`**——
`raw_text`、`jd_text`、`parsed`、整批 `questions` 都在里面。它是全仓最大的单一真相冗余。

**默认 dry-run**，和 `reap_tasks` 同一条理由：它删的是别人写下的历史，判据要留在能看见全局的人手上。

判据按**认领关系**分四档（认领 = 这个线程属于哪份报告）：

- `protected`：认领它的报告还在 `running`。删了它，正在跑的那次运行的下一步就找不到父 checkpoint。
- `prune`：认领它的报告已是终态（`completed` / `aborted`），且**距完成时刻超过 `--keep-days`**。
  完成时刻取 `interview.updated_at`——收尾那次写库就是它最后一次变化，与 checkpoint 里
  最后一个 id 的时间实测同秒（本机 13 份全部对得上）。
- `keep`：终态但还在保留窗口内。
- `orphan`：**没有任何报告认领它**。默认只报数、不删，要 `--include-orphans` 才动手。

`orphan` 单独一档是实测逼出来的，不是谨慎过头：T-304 之前落库的报告行里 `thread_id` 是空串，
它们的线程只能靠 checkpoint 正文里的 `interview_id` 认领（本脚本会做这一次认领，
与 `export_report_md.py` 的旧行兜底同一个判据）。不认回来的那些才是真无主——
通常是报告被删了。反过来说，把"行上没写线程名"直接当成垃圾，会一次性废掉本机
**13 份历史报告**重新导出图终态（五维评分、配额表）的能力。

删的动作只走 `AsyncSqliteSaver.adelete_thread()`：它同时清 `checkpoints` 与 `writes` 两张表并提交。
自己写 `DELETE FROM checkpoints` 会留下 `writes` 里的孤儿行，而 langgraph 的表结构没有兼容性承诺。

`--apply` 之后文件**不会自己变小**：SQLite 的 DELETE 只把页还给 freelist。
要真的缩文件就加 `--vacuum`（它独占写锁，别在服务还在跑的时候用）。
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents import workflow
from app.agents.workflow import open_checkpointer, read_thread_state
from app.config import get_settings
from app.dependencies import dispose_engine, get_sessionmaker
from app.models.base import utcnow_naive
from app.repositories.interview_repository import InterviewRepository
from app.utils.db import session_scope

#: 默认保留窗口（天）。取 90 不是为了省空间，是为了"这一卷大概还会被重新导出"这件事：
#: 报告正文与题目都在业务库里，prune 之后导出仍然成功（`tests/test_scripts/
#: test_export_report_md.py::test_a_report_whose_checkpoint_is_gone_still_exports`），
#: 少的只是只有图终态才有的那几节（五维评分、配额表、耗时）。
#: 给 0 就是"全部终态线程都删"，那是显式授权，不是默认。
DEFAULT_KEEP_DAYS = 90

#: `ThreadAudit.verdict` 的四个取值。写成常量是因为 `--include-orphans` 要按名字筛。
PROTECTED = "protected"
PRUNE = "prune"
KEEP = "keep"
ORPHAN = "orphan"

#: SQLite 的 UUIDv6 epoch（1582-10-15）与 Unix epoch 的秒差。langgraph 给每个
#: checkpoint 发的 id 是 UUIDv6，时间就编码在 id 里。
_UUID6_EPOCH_OFFSET_SECONDS = 12_219_292_800


@dataclass(frozen=True)
class Report:
    """认领线程所需的最小报告事实。不传 ORM 实例出事务：`session_scope` 一退出列就过期。"""

    id: str
    status: str
    thread_id: str
    updated_at: datetime


@dataclass(frozen=True)
class ThreadAudit:
    """一个 checkpoint 线程的体检结果。"""

    thread_id: str
    rows: int
    #: 认领它的报告 id；空串 = 没有报告认领它
    interview_id: str
    #: 那份报告的 `status`；空串同上
    status: str
    #: 这个线程最后一次写 checkpoint 的时刻（取自最后一个 `checkpoint_id`）
    last_run_at: datetime
    #: 距"锚点时刻"多少天：有报告就是报告的完成时刻，无主才是线程自己的时间
    age_days: int
    verdict: str


def checkpoint_db_path() -> Path:
    """checkpoint 库的位置。

    **每次调用都从模块属性取**，不在 import 时抓值：`read_thread_state` 默认打开的就是
    `workflow.CHECKPOINT_DB`，测试把它指到 `tmp_path` 时清单、认领、删除必须跟着一起挪。
    """
    return workflow.CHECKPOINT_DB


def _thread_inventory(path: Path) -> dict[str, tuple[int, datetime]]:
    """线程 → (行数, 最后一次写入时刻)。只读连接（`mode=ro`）。

    行数取 `checkpoints` 一张表就够了，它是"这次跑了多少步"的口径；
    `writes` 是它的附属，删的时候由 `adelete_thread` 一起清。
    `max(checkpoint_id)` 按文本比就是按时间比：UUIDv6 把时间戳放在最高位，
    十六进制串的字典序与时间序同构（实测本机 13 个线程逐个核对）。
    """
    if not path.is_file():
        return {}
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "select thread_id, count(*), max(checkpoint_id) from checkpoints group by thread_id"
        )
        inventory: dict[str, tuple[int, datetime]] = {}
        for thread, count, latest_id in rows:
            inventory[str(thread)] = (int(count), _checkpoint_time(str(latest_id)))
        return inventory
    finally:
        connection.close()


def _checkpoint_time(checkpoint_id: str) -> datetime:
    """从 `checkpoint_id` 解出写入时刻（naive UTC，与业务库的时间戳同口径）。

    版本不对就抛，**不猜**：拿不到年龄就没有"超过保留窗口"可言，而判错的代价是删数据。
    """
    parsed = uuid.UUID(checkpoint_id)
    if parsed.version != 6:
        raise ValueError(
            f"checkpoint id {checkpoint_id!r} 不是 UUIDv6（版本 {parsed.version}），"
            "解不出线程年龄；宁可不动，也不靠猜的时钟删数据"
        )
    return datetime.fromtimestamp(parsed.time / 1e7 - _UUID6_EPOCH_OFFSET_SECONDS, UTC).replace(
        tzinfo=None
    )


async def _reports(sessionmaker: async_sessionmaker[AsyncSession]) -> list[Report]:
    """库里的全部报告行。

    先 `count()` 再按它取，是因为**漏一份报告比多读几行严重得多**：没列出来的报告会让
    它名下的线程看起来无主，于是被当成垃圾删掉。分页上限在这里不是性能参数，是正确性参数。
    """
    async with session_scope(sessionmaker) as session:
        repository = InterviewRepository(session)
        total = await repository.count()
        rows = await repository.list(limit=total) if total else []
        return [
            Report(
                id=row.id,
                status=row.status,
                thread_id=row.thread_id,
                updated_at=row.updated_at,
            )
            for row in rows
        ]


async def classify(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    keep_days: int = DEFAULT_KEEP_DAYS,
    checkpoint_path: Path | None = None,
) -> list[ThreadAudit]:
    """给 checkpoint 库里的每个线程判一档。**只读，不删任何东西。**

    认领的顺序有意义：先看行上的 `thread_id`（T-304 之后的新报告，一次哈希查表），
    认不上的才去读 checkpoint 正文里的 `interview_id`（旧报告，一次 `aget_state`）。
    反过来的话新报告也要付扫描的代价，那正是 T-304 要省掉的东西。
    """
    path = checkpoint_path if checkpoint_path is not None else checkpoint_db_path()
    inventory = _thread_inventory(path)
    if not inventory:
        return []
    reports = await _reports(sessionmaker)
    by_thread = {report.thread_id: report for report in reports if report.thread_id}
    by_id = {report.id: report for report in reports}

    now = utcnow_naive()
    audits: list[ThreadAudit] = []
    for thread_id, (rows, last_run_at) in sorted(inventory.items()):
        report = by_thread.get(thread_id)
        if report is None:
            state = await read_thread_state(thread_id, checkpoint_path=path)
            report = by_id.get(str(state.get("interview_id") or ""))
        anchored = report.updated_at if report is not None else last_run_at
        age_days = int((now - anchored).total_seconds() // 86_400)
        if report is None:
            verdict = ORPHAN
        elif report.status == "running":
            verdict = PROTECTED
        elif age_days >= keep_days:
            verdict = PRUNE
        else:
            verdict = KEEP
        audits.append(
            ThreadAudit(
                thread_id=thread_id,
                rows=rows,
                interview_id=report.id if report is not None else "",
                status=report.status if report is not None else "",
                last_run_at=last_run_at,
                age_days=age_days,
                verdict=verdict,
            )
        )
    return audits


def _chosen(audits: Sequence[ThreadAudit], *, include_orphans: bool) -> list[ThreadAudit]:
    """这一轮真要删的线程。无主的那一档只有显式授权才进来。"""
    wanted = {PRUNE}
    if include_orphans:
        wanted.add(ORPHAN)
    return [audit for audit in audits if audit.verdict in wanted]


def _format(
    audits: Sequence[ThreadAudit],
    chosen: Sequence[ThreadAudit],
    *,
    applied: bool,
    include_orphans: bool,
    vacuum_saved: int | None,
) -> str:
    """把体检结果摊成给人看的清单。只报 id、行数与年龄，不打印任何 checkpoint 正文。"""
    counts: dict[str, int] = {}
    for audit in audits:
        counts[audit.verdict] = counts.get(audit.verdict, 0) + 1
    lines = [
        f"线程 {len(audits)} 个："
        f"prune={counts.get(PRUNE, 0)} keep={counts.get(KEEP, 0)}"
        f" protected={counts.get(PROTECTED, 0)} orphan={counts.get(ORPHAN, 0)}"
    ]
    if chosen:
        head = "已删除" if applied else "将删除（dry-run，加 --apply 才动手）"
        lines.append(f"{head}：{len(chosen)} 个线程")
        for audit in chosen:
            who = (
                f"report={audit.interview_id[:12]}/{audit.status}" if audit.interview_id else "无主"
            )
            lines.append(
                f"  {audit.thread_id[:12]}  {who}  checkpoint {audit.rows} 行"
                f"  {audit.age_days} 天前"
            )
        if applied and vacuum_saved is not None:
            lines.append(f"VACUUM 回收 {vacuum_saved:,} 字节")
        elif applied:
            lines.append("提示：DELETE 只把页还给 freelist，文件不会变小；要缩文件加 --vacuum")
    else:
        lines.append("没有可删的线程（保留窗口之外的一份也没有）")

    # 无主那一档只在人显式授权时才动，所以**每一轮都要把入口指出来**——
    # 只在"什么都不用删"时提一次，会让人以为 orphan=3 那行是脚本自己会处理的。
    guarded = counts.get(ORPHAN, 0)
    if guarded and not include_orphans:
        lines.append(f"无主的 {guarded} 个不在上面这份清单里：要删它们得加 --include-orphans")
    return "\n".join(lines)


def _vacuum(path: Path) -> int:
    """整理文件，返回缩小的字节数。

    `isolation_level=None` 是必须的：SQLite 不许 `VACUUM` 跑在事务里，
    而默认隔离级别下 python 的 sqlite3 会在写语句前偷偷 BEGIN。
    """
    before = path.stat().st_size
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("VACUUM")
    finally:
        connection.close()
    return max(before - path.stat().st_size, 0)


async def prune(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    keep_days: int = DEFAULT_KEEP_DAYS,
    apply: bool = True,
    include_orphans: bool = False,
    vacuum: bool = False,
    checkpoint_path: Path | None = None,
) -> list[ThreadAudit]:
    """判档 → （`apply` 才）删除 → 打印。返回这一轮被认定该删的线程，供测试断言。"""
    path = checkpoint_path if checkpoint_path is not None else checkpoint_db_path()
    audits = await classify(sessionmaker, keep_days=keep_days, checkpoint_path=path)
    chosen = _chosen(audits, include_orphans=include_orphans)
    vacuum_saved: int | None = None
    if chosen and apply:
        async with open_checkpointer(path) as saver:
            for audit in chosen:
                await saver.adelete_thread(audit.thread_id)
        if vacuum:
            vacuum_saved = _vacuum(path)
    print(
        _format(
            audits,
            chosen,
            applied=apply,
            include_orphans=include_orphans,
            vacuum_saved=vacuum_saved,
        )
    )
    if audits:
        print(f"checkpoint 库：{path.as_posix()}（{path.stat().st_size:,} 字节）")
        print(f"业务库：{get_settings().database_url}")
    return chosen


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按报告清理 checkpoint 库里旧线程的运行痕迹")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正删除线程的 checkpoint（默认只打印将删清单）",
    )
    parser.add_argument(
        "--keep-days",
        type=int,
        default=DEFAULT_KEEP_DAYS,
        help=f"终态报告完成之后再多留几天（默认 {DEFAULT_KEEP_DAYS}）；给 0 表示不留窗口",
    )
    parser.add_argument(
        "--include-orphans",
        action="store_true",
        help="连没有报告认领的线程一起删（旧报告靠它保住图终态，默认不删）",
    )
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="删完整理文件；只在 --apply 时生效，且要求没有别的进程在写这个库",
    )
    return parser.parse_args(argv)


async def _main(argv: Sequence[str] | None) -> None:
    args = _parse_args(argv)
    try:
        await prune(
            get_sessionmaker(),
            keep_days=args.keep_days,
            apply=args.apply,
            include_orphans=args.include_orphans,
            vacuum=args.vacuum,
        )
    finally:
        # 脚本与 API 共用 `dependencies` 里那台缓存引擎；不 dispose 就把连接池吊在进程里
        await dispose_engine()


def main(argv: Sequence[str] | None = None) -> None:
    asyncio.run(_main(argv))


if __name__ == "__main__":
    main()
