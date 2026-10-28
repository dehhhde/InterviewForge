"""`LLMClient` 的生命周期收尾〔T-011 / BACKLOG P1-6〕。

`get_llm()` 每请求新建（AGENTS.md §3 的铁律），但同步 `def` 依赖没有 yield 点，
`AsyncOpenAI` 的连接池只能靠 GC。这里钉两件事：

1. 真实依赖走完后，收尾**必然**调用 `aclose()`——哪怕端点返回 422 也要关；
2. 既有形状不受影响：`app.dependency_overrides[get_llm]` 继续接受一个返回客户端的
   同步 callable（`test_job_image_api.py` / `test_upload_limits.py` 都这么写）。

只断"关没关"与状态码，不比模型文案（AGENTS.md §6）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.dependencies import dispose_engine, get_engine, get_llm
from app.main import create_app
from app.models import Base
from app.utils.llm_client import LLMClient

PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60


@pytest.fixture(autouse=True)
def _settings_point_at_this_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    url = f"sqlite+aiosqlite:///{(tmp_path / 'llmlife.db').as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    await dispose_engine()
    engine = get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    built = create_app()
    yield built
    await dispose_engine()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://if.test") as new:
        yield new


async def test_get_llm_closes_the_client_after_the_request(
    app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[LLMClient] = []
    original = LLMClient.aclose

    async def spy(self: LLMClient) -> None:
        closed.append(self)
        await original(self)

    monkeypatch.setattr(LLMClient, "aclose", spy)
    monkeypatch.setattr(get_settings(), "llm_model_vision", "")

    response = await client.post(
        "/api/v1/jobs/inspect-image", files={"file": ("poster.png", PNG_HEADER, "image/png")}
    )

    # 没配视觉模型 → 422（`parse_vision_unconfigured`），但收尾照样执行
    assert response.status_code == 422
    assert len(closed) == 1


async def test_aclose_is_idempotent_and_closes_the_underlying_pool() -> None:
    """`aclose()` 幂等，且把底层客户端一起关掉；假件没有 `aclose` 也不炸。"""

    class _RecordingPool:
        def __init__(self) -> None:
            self.aclose_calls = 0

        async def aclose(self) -> None:
            self.aclose_calls += 1

    class _SyncOnly:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    pool = _RecordingPool()
    client = LLMClient(settings=get_settings().model_copy(deep=True), client=pool)
    await client.aclose()
    await client.aclose()
    assert pool.aclose_calls == 1

    sync_only = _SyncOnly()
    client2: Any = LLMClient(settings=get_settings().model_copy(deep=True), client=sync_only)
    await client2.aclose()
    assert sync_only.close_calls == 1

    # 什么都不实现的注入件：只记标记，不抛
    class _Opaque:
        pass

    client3 = LLMClient(settings=get_settings().model_copy(deep=True), client=_Opaque())
    await client3.aclose()


async def test_sync_override_still_satisfies_llmdep(
    app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既有测试形状不破：override 一个返回客户端的同步 lambda，端点照常工作。"""

    class _Never:
        async def create(self, **kwargs: Any) -> Any:
            raise AssertionError("不该打到模型")

    app.dependency_overrides[get_llm] = lambda: LLMClient(
        settings=get_settings().model_copy(deep=True), client=_Never()
    )
    monkeypatch.setattr(get_settings(), "llm_model_vision", "some-vision-model")

    # 图太小/OCR 替身不回内容都会失败，但绝不能因为依赖形状不兼容变成 500
    response = await client.post(
        "/api/v1/jobs/inspect-image", files={"file": ("poster.png", PNG_HEADER, "image/png")}
    )
    assert response.status_code != 500
