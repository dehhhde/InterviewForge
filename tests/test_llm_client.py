"""`LLMClient` 的失败分类〔阶段 4 · §7.3，M6 补上游状态码这一支〕

单测只关心一件事：**异常出门时是不是 `AppError` 家族**。后台任务把失败记进 `task.error`
时按 `AppError.code` 归因（`services/task_service.mark_failed`），非 `AppError` 一律
`internal_error`——所以"账户余额不足"这种一眼能修的毛病若以裸 `APIStatusError` 冒出来，
轮询方就只能看见一句 `APIStatusError: ...`，不知道该干什么（实测 2026-10-20 撞到 402）。

断言全在调用次数与 code 上，不看任何文案。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import APIConnectionError, APIStatusError

from app.config import get_settings
from app.exceptions.base import LLMError
from app.utils.llm_client import LLMClient


class _RaisingCompletions:
    """只实现 `chat.completions.create`，每次调用都抛同一个异常，并记次数。"""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        raise self.exc


def _client(raising: _RaisingCompletions) -> LLMClient:
    settings = get_settings()
    # 传输重试次数压到 1：测试要的是"耗尽之后成什么形状"，不是等指数退避睡够
    return LLMClient(settings, client=raising)


def _status_error(code: int) -> APIStatusError:
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    response = httpx.Response(code, request=request)
    return APIStatusError("Insufficient Balance", response=response, body=None)


@pytest.fixture(autouse=True)
def _single_transport_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings().llm, "transport_max_attempts", 1)


async def test_an_exhausted_transport_failure_becomes_llm_error() -> None:
    raising = _RaisingCompletions(APIConnectionError(request=_request()))
    client = _client(raising)

    with pytest.raises(LLMError) as caught:
        await client.chat("extract", [{"role": "user", "content": "hi"}])

    assert len(raising.calls) == 1
    assert client.usage.failed_calls == 1
    assert client.usage.calls == 0  # 失败的调用不进 token 账，否则成本会被低估
    assert caught.value.code == "llm_error"
    assert caught.value.details["attempts"] == 1


async def test_an_upstream_status_error_is_classified_and_not_retried() -> None:
    """402/401 这类"重发也不会好"的响应：**不重试**，但必须换成 `LLMError` + 状态码。"""
    raising = _RaisingCompletions(_status_error(402))
    client = _client(raising)

    with pytest.raises(LLMError) as caught:
        await client.chat("generate", [{"role": "user", "content": "hi"}])

    assert len(raising.calls) == 1
    assert caught.value.details["status_code"] == 402
    assert caught.value.code == "llm_error"


async def test_a_rate_limit_goes_down_the_transport_branch_not_the_status_one() -> None:
    """`RateLimitError` 也是 `APIStatusError` 的子类，但它归传输重试——分支顺序不能反。"""
    from openai import RateLimitError

    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    response = httpx.Response(429, request=request)
    raising = _RaisingCompletions(RateLimitError("slow down", response=response, body=None))

    with pytest.raises(LLMError) as caught:
        await _client(raising).chat("review", [{"role": "user", "content": "hi"}])

    # 走传输分支的细节：`details` 给的是 attempts，而不是 status_code
    assert caught.value.details["attempts"] == 1
    assert "status_code" not in caught.value.details


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://api.deepseek.com/chat/completions")
