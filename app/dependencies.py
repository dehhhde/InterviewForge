"""依赖注入：get_db / get_llm / get_current_user / get_page_params〔阶段 2 · §5.3〕

这里只做**装配**：进程级的引擎单例 + 会话工厂 + `get_db` 依赖 + HTTP 侧的 LLM 客户端。
`get_current_user` 与 `get_page_params` 属认证与分页，等 M4 的 API 层落地再补——
现在写了也没有调用点，只会变成"看起来有人用"的死代码。

引擎必须是单例：`build_engine` 会注册 connect 事件监听并打开 WAL，一个进程里建两台引擎
就是两套连接池去抢同一个 SQLite 文件（单写者），而 `busy_timeout` 只让第二个池排队等待。

`get_db` 提交事务，repository 只 flush——分工写进 `AGENTS.md` §3，别在下游改判。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import get_settings
from app.utils.db import build_engine, build_sessionmaker, session_scope
from app.utils.llm_client import LLMClient

_engine: AsyncEngine | None = None
_engine_url: str | None = None


def get_engine() -> AsyncEngine:
    """进程内唯一的异步引擎，URL 取自 `settings.database_url`。

    缓存按 URL 失效：测试用 `monkeypatch.setattr(settings, "database_url", …)` 换到
    `tmp_path` 下的临时库时，必须看到新库，而不是启动时缓存的那台引擎。
    """
    global _engine, _engine_url

    url = get_settings().database_url
    if _engine is not None and _engine_url == url:
        return _engine

    if _engine is not None:
        # `close=False`：异步上下文里替换引擎的推荐做法，同步关池会阻塞事件循环。
        # 遗留连接交给 GC，测试进程结束时一并回收。
        _engine.sync_engine.dispose(close=False)
    _engine = build_engine(url)
    _engine_url = url
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """会话工厂，挂在 `get_engine()` 那台引擎上。

    工厂本身不缓存：它只是"引擎 + 两个固定参数"的薄封装，缓存它反而多一份要一起失效的状态。
    """
    return build_sessionmaker(get_engine())


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：一个请求一个事务。

    端点正常返回才提交；端点抛异常（含 `HTTPException`）时回滚后原样抛出，
    于是"半途失败"的写入不会留在库里。提交点在这里而不是 repository 里，
    是因为一次业务写入可能要跨多张表（一份报告 = `interview` + N 行 `question`）。
    """
    async with session_scope(get_sessionmaker()) as session:
        yield session


async def dispose_engine() -> None:
    """关闭连接池。给 M4 的 lifespan 收尾用，测试之间也拿它复位单例。"""
    global _engine, _engine_url

    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _engine_url = None


#: 端点签名用的会话依赖。写成 `SessionDep` 别名而不是每处 `Depends(get_db)`——
#: 后者在参数默认值里会撞 ruff B008（函数默认值里调函数）。
SessionDep = Annotated[AsyncSession, Depends(get_db)]

#: 要给 `BackgroundTasks` 排活的端点用这个，不是 `SessionDep`。
#: 实测（FastAPI 0.143 + Starlette 1.8，`scripts/probe_background_order.py`）：
#: 后台协程在**依赖收尾之前**运行，顺序是 `dep-enter → endpoint → background → dep-exit`。
#: 于是 `get_db` 的提交发生在后台开跑之后，后台第一件事就是"行不存在"。这类端点必须
#: 自己在返回前把行提交掉，所以注入的是工厂而不是请求会话（ADR-0011）。
SessionMakerDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)]


def get_llm() -> LLMClient:
    """HTTP 侧的 LLM 客户端装配点。与引擎不同，它**每次请求新建**，原因有两条：

    - 客户端里的 `Semaphore` 与 `UsageTotals` 都是"一次运行"的量纲。缓存成进程单例，
      用量统计就会跨请求累加，而成本护栏（ADR-0011）说的是"这一次生成跑烧了多少"。
    - 测试要换替身。走 `app.dependency_overrides[get_llm]` 比 monkeypatch 服务层内部干净，
      和 `tests/test_api/` 现有的夹具同形。

    分层铁律照旧：节点、服务与抽取器内部都不自己 new client，装配期把它传下去。
    """
    return LLMClient(get_settings())


LLMDep = Annotated[LLMClient, Depends(get_llm)]
