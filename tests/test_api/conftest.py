"""`tests/test_api/` 共用的 ASGI 夹具（T-018 收敛：以前四个文件各抄了一份）。

这里只管"装配一台真应用"这一件事：把进程级的真相（数据库 URL、上传目录）指到本用例的
`tmp_path`，用**单例引擎**建表，再交一份真 `create_app()` 出去。四个文件各自多出来的需求
不往这里塞，走钩子：

- 还要改配置 → 在自己文件里再加一个 autouse fixture，继续 `monkeypatch.setattr`。
  conftest 的 autouse 先跑，本文件的后跑，改的是不同属性，不存在谁覆盖谁；
- 要换依赖 → 覆盖 `app_overrides`，**别覆盖 `app`**。

装配保持"真"是因为这些用例钉的就是接缝：`BackgroundTasks` 跑在依赖收尾之前（ADR-0011）、
409 是先查后插判出来的（ADR-0013）、缺视觉模型要显式拒绝（ADR-0014）——换成假应用全都测不到。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.dependencies import dispose_engine, get_engine
from app.main import create_app
from app.models import Base


@pytest.fixture(autouse=True)
def _settings_point_at_this_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """库与上传目录都指到本用例的 `tmp_path`。

    上传目录不隔离就会写进 `data/uploads/`——测试顺手往开发目录灌垃圾是迟早的事。
    库文件名统一叫 `test.db`：区分用例的是**目录**而不是文件名（`tests/conftest.py` 同一口径）。
    """
    settings = get_settings()
    url = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")


@pytest.fixture
def app_overrides() -> dict[Any, Any]:
    """要换掉某个依赖的文件覆盖这里；`get_llm` 的替身就是从这儿进来的。"""
    return {}


@pytest.fixture
async def app(app_overrides: dict[Any, Any]) -> AsyncIterator[FastAPI]:
    """建好表的真应用。**引擎用单例**：另起一台就是第二个连接池抢同一个 SQLite 文件。"""
    await dispose_engine()
    engine = get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    built = create_app()
    built.dependency_overrides.update(app_overrides)
    yield built
    await dispose_engine()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://if.test") as new:
        yield new
