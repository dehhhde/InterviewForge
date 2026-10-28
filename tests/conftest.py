"""数据库侧的四个 fixture + 整套测试的环境锚点〔阶段 1 · §4.7 / S3 · T-313〕

**`APP_ENV` 在这里定成 `testing`**（模块级，早于任何用例与任何 `get_settings()`）：
在此之前 `configs/testing.yaml` 是一份"看着在生效"的配置——`.env` 写着 development，
没有任何地方设过 `APP_ENV`，所以几百条用例解析的一直是 development.yaml。
切过来之后 yaml 里那些为测试准备的窄参数（`request_timeout_seconds=5`、
`retry_deadline_seconds=60`、`upload.*`、`question.total=3`）才真的被用到，
"测试不许产生真实 LLM 请求"也第一次有了能被读出来的参数依据。

代价是**用例不能再靠环境的宽**：依赖 `llm.max_concurrency` 默认 4 的用例必须自己声明。
实测只有一条红（`tests/test_parsers.py::test_concurrent_pages_keep_page_order`，
三页 OCR 的完成顺序要三次同时在飞），已改成 `model_copy(deep=True)` +
`monkeypatch.setattr(settings.llm, "max_concurrency", 3)`；它的姊妹用例
`test_pages_reach_the_model_together_not_one_after_another` 早就自己放大过信号量，
注释里还写着"可能是 2"——那条注释等的就是这次切换。这类"参数来自环境"的依赖
本来就是隐患，切环境只是把它照出来。

为什么不在 `get_settings()` 里判"现在是测试"：那会让生产代码读 pytest 的环境变量，
分层反了。`lru_cache` 清一次是因为 `os.environ` 在 conftest 之前可能已经被别的入口读过。

LLM 的替身**不在这里**——它在 `tests/test_agents/_fake_llm.py`（`FakeCompletions`，
T-018 之后是全仓唯一一份）。把替身做成 autouse fixture 反而挡掉"注入不同回复形状"的用例，
所以按现有惯例由各文件自己装配。

样本简历也不是 fixture，是**文件**：`tests/fixtures/resumes/`（真简历，缺文件就 skip）与
`tests/fixtures/resume_ok.json`（schema 用例的 payload）。原先这个 docstring 宣传过一个
叫 `sample_resume` 的 fixture——它从来没存在过，别照着它找。

数据库侧的 fixture 只有一种形态：**每个测试一个独立的临时文件库**。

- 不用 `:memory:`——SQLite 内存库是"每个连接一份库"，异步引擎的连接池一换连接，
  上一个连接建好的表就消失了（`app/utils/db.py` 里对此有显式拒绝）。
- 不用共享的 `data/interview_forge.db`——那会让测试之间互相看见，
  也让"跑测试顺手把开发库清了"成为可能。M3 验收里"互不干扰"就是靠 `tmp_path`
  （pytest 的目录按测试函数唯一）保证的。所以库文件名统一叫 `test.db`：**区分用例的是
  目录而不是文件名**，失败时看 `tmp_path` 那行就知道是哪个用例留下的。
- 建表走 `Base.metadata.create_all`，**不走 alembic**。迁移本身有专门的测试
  （`tests/test_migrations.py` 用 subprocess 跑 `alembic upgrade head`），
  别让几百条用例都去付一次迁移的成本。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import get_settings
from app.models import Base
from app.utils.db import build_engine, build_sessionmaker

#: 环境变量压过 `.env`（pydantic-settings 的优先级，见 `app/config.py::_env_name_from_runtime`），
#: 所以这一行就足以让整套测试解析 `configs/testing.yaml`，不必碰 `.env`（AGENTS.md §6：别改它）。
os.environ["APP_ENV"] = "testing"
get_settings.cache_clear()


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    """本测试独占的 sqlite 文件。文件名带上测试 id，失败时一眼能认出是哪个用例留下的。"""
    return f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    """建好表的异步引擎。建表用同步 `create_all`，通过 `run_sync` 塞进同一个连接。"""
    built = build_engine(db_url)
    async with built.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield built
    await built.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """一个事务内的会话：测试结束统一回滚，脏数据不会活到下一个用例。

    需要验证"提交之后还能读到"的用例请直接用 `session_scope`，别在这里 commit。
    """
    async with AsyncSession(engine, expire_on_commit=False) as new:
        yield new
        await new.rollback()


@pytest.fixture
def db_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """提交型会话工厂，给"commit 之后还能不能读到"这类用例用。"""
    return build_sessionmaker(engine)
