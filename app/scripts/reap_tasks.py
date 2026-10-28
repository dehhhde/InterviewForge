"""僵尸任务回收 CLI〔T-015 / BACKLOG OPT-4.1〕

跑法（`PYTHONPATH=.` 不能省，`app.scripts` 不是已安装包）：

    PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/Scripts/python.exe -m app.scripts.reap_tasks
    PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/Scripts/python.exe -m app.scripts.reap_tasks --apply

**默认 dry-run**：它要改的是别人的终态。一行 `running` 停在库里有很多成因
（进程重启、写库失败、甚至只是某个任务真的跑得很久），把判断权留在能看见全局的人手上，
而不是留在一个定时任务里——`--apply` 才是显式授权。反复跑幂等：翻完的行不再是 `running`。

阈值取 `configs/<APP_ENV>.yaml` 的 `task.stale_after_minutes`，`--minutes` 临时覆盖
（排查时给个小值看判据长什么样，别拿它当常态）。

为什么只有 CLI 没有 HTTP 入口：`POST /tasks/reap` 是个"任何人不加鉴权就能改库"的写端点，
而 D-7（单机自用还是多租户）还没定。定成多租户之前，这条先只有拿着终端的人能按。
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.dependencies import dispose_engine, get_sessionmaker
from app.services.task_service import StaleTask, reap_stale


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回收停在 running 的僵尸任务行")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正翻成 failed（默认只打印将翻的清单）",
    )
    parser.add_argument(
        "--minutes",
        type=int,
        default=None,
        help="停滞阈值（分钟），默认取配置 task.stale_after_minutes",
    )
    return parser.parse_args(argv)


def _format(rows: Sequence[StaleTask], *, applied: bool) -> str:
    """把清单摊成给人看的几行。库里只有任务 id 与业务 id，不打印任何正文。"""
    if not rows:
        return "没有可清理的僵尸任务（库里没有停在 running 且超过阈值的行）"
    head = "已翻成 failed" if applied else "将翻成 failed（dry-run，加 --apply 才动手）"
    lines = [f"{head}：{len(rows)} 行"]
    for row in rows:
        lines.append(
            f"  {row.task_id}  kind={row.kind}  status={row.status}"
            f"  ref={row.ref_id or '-'}  停滞 {row.stalled_minutes} 分钟"
        )
    return "\n".join(lines)


async def reap(
    sessionmaker: async_sessionmaker[AsyncSession], *, apply: bool, minutes: int | None
) -> list[StaleTask]:
    """跑一次回收并打印。返回被认定僵尸的行——测试注入自己的临时库。"""
    rows = await reap_stale(sessionmaker, minutes=minutes, apply=apply)
    print(_format(rows, applied=apply))
    if rows:
        print(f"库：{get_settings().database_url}")
    return rows


async def _main(argv: Sequence[str] | None) -> None:
    args = _parse_args(argv)
    try:
        await reap(get_sessionmaker(), apply=args.apply, minutes=args.minutes)
    finally:
        # 脚本与 API 共用 `dependencies` 里那台缓存引擎；不 dispose 就把连接池吊在进程里
        await dispose_engine()


def main(argv: Sequence[str] | None = None) -> None:
    asyncio.run(_main(argv))


if __name__ == "__main__":
    main()
