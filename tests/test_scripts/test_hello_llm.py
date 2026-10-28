"""`hello_llm` 自检脚本的离线用例〔T-018 / BACKLOG P3-8〕

脚本本身要联网，所以这里只验**形状**：走 `LLMClient`、`max_tokens` 掐在小值、
异常不被吞、底层客户端被关。断言全落在调用参数与抛出行为上，不碰模型文案（AGENTS.md §6）。

这条脚本 2026-10-27 之前只有一行 docstring——跑它什么都不做还退出 0，所以它的用例
首先得是一条"确实发出了一次请求"的用例。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import get_settings
from app.scripts.hello_llm import _MAX_TOKENS, main, run
from app.utils.llm_client import LLMClient
from tests.test_agents._fake_llm import FakeCompletions


def _client(fake: Any) -> LLMClient:
    return LLMClient(get_settings(), client=fake)


async def test_run_sends_exactly_one_capped_request() -> None:
    fake = FakeCompletions(["好"])
    await run(_client(fake))

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["model"] == get_settings().llm_model_extract
    assert call["max_tokens"] == _MAX_TOKENS


async def test_a_failing_upstream_is_not_swallowed() -> None:
    """这条是本卡的正证：静默成功是原空壳唯一的危害，异常必须原样抛出。"""
    fake = FakeCompletions(raises=ValueError("上游 500"))
    with pytest.raises(ValueError, match="上游 500"):
        await run(_client(fake))


async def test_main_closes_the_underlying_client(capsys: pytest.CaptureFixture[str]) -> None:
    class _Closable(FakeCompletions):
        def __init__(self) -> None:
            super().__init__(["好"])
            self.close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1

    fake = _Closable()
    await main(_client(fake))

    assert fake.close_calls == 1
    printed = capsys.readouterr().out
    assert get_settings().llm_model_extract in printed
