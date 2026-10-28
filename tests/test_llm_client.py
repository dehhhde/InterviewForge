"""`LLMClient` 的失败分类〔阶段 4 · §7.3，M6 补上游状态码这一支，S2 补重试预算〕

单测只关心一件事：**异常出门时是不是 `AppError` 家族**。后台任务把失败记进 `task.error`
时按 `AppError.code` 归因（`services/task_service.mark_failed`），非 `AppError` 一律
`internal_error`——所以"账户余额不足"这种一眼能修的毛病若以裸 `APIStatusError` 冒出来，
轮询方就只能看见一句 `APIStatusError: ...`，不知道该干什么（实测 2026-10-20 撞到 402）。

断言全在调用次数与 code 上，不看任何文案。

下面还管着两层重试的**预算形状**（T-206/T-207）：并发槽在退避期间必须是空的、
校验重试回灌的原文必须被截断、总时限越线必须换成 `llm_timeout`。三条都是结构断言。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, RateLimitError

from app.config import Settings, get_settings
from app.exceptions.base import LLMError, LLMResponseError
from app.utils.llm_client import LLMClient
from tests.test_agents._fake_llm import FakeCompletions


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


# ---------------------------------------------------------------- 两层重试的预算（T-206/T-207）


def _scoped_settings(**llm_params: int) -> Settings:
    """深拷贝配置并只改 `llm:` 段的这几个数。

    这几条用例改的是**预算**（槽数、截断字数、总时限），不动 `get_settings()` 那个单例：
    单例被改了同进程别的用例跟着变（AGENTS.md §6）。
    """
    copy = get_settings().model_copy(deep=True)
    for name, value in llm_params.items():
        setattr(copy.llm, name, value)
    return copy


class _SignallingFake(FakeCompletions):
    """共享替身 + "第一次尝试已经开始"的事件，用来把退避窗口卡在可断言的位置。"""

    def __init__(self, replies: list[Any]) -> None:
        super().__init__(replies)
        self.first_attempt_started = asyncio.Event()

    async def create(self, **kwargs: Any) -> Any:
        if not self.calls:
            self.first_attempt_started.set()
        return await super().create(**kwargs)


class _HangingCompletions:
    """每次都卡在永不落地的事件上：模拟"上游收了请求但不回"，只能靠总时限脱身。"""

    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=self)
        self.calls = 0

    async def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        await asyncio.Event().wait()
        raise AssertionError("不会走到这里")


def _tagged(tag: str) -> list[dict[str, str]]:
    """给并发用例用的最小消息：`content` 就是"这是哪一个调用"的标识。"""
    return [{"role": "user", "content": tag}]


async def test_a_backing_off_call_hands_its_concurrency_slot_back() -> None:
    """〔T-207 / BACKLOG P2「信号量包住整个 tenacity 循环」〕

    只有 1 个槽：第一次尝试抛 `RateLimitError` 之后进入指数退避（实测首个等待 ~0.5s）。
    acquire 若还在重试循环外面，第二个调用要等第一个整个跑完才拿得到槽；挪进循环之后
    退避那几秒槽是空的，所以**第二个调用必须插在第一次的重试之前**发出去。
    断言落在 `create` 的记录顺序上——那正是"谁在推进"的事实。
    """
    rate_limited = RateLimitError(
        "slow down", response=httpx.Response(429, request=_request()), body=None
    )
    fake = _SignallingFake([rate_limited, "ok", "ok"])
    client = LLMClient(
        _scoped_settings(max_concurrency=1, transport_max_attempts=3, retry_deadline_seconds=600),
        client=fake,
    )

    first = asyncio.create_task(client.chat("generate", _tagged("first")))
    await fake.first_attempt_started.wait()
    second = asyncio.create_task(client.chat("generate", _tagged("second")))
    await second

    assert fake.calls[-1]["messages"][-1]["content"] == "second"
    assert len(fake.calls) == 2

    await first
    assert [call["messages"][-1]["content"] for call in fake.calls] == ["first", "second", "first"]


async def test_the_refeed_history_is_clipped_to_the_cap() -> None:
    """〔T-206 / BACKLOG P2「校验重试把完整原始输出塞回对话」〕

    三轮问答里，回灌给模型的上一轮输出必须是被截过的一小段，且消息条数每轮只加 2
    （assistant + user）。不断截断标记的文案，只断"前缀还在、长度变小了"。
    """
    broken = "{" + "x" * 4000
    fake = FakeCompletions([broken])
    client = LLMClient(_scoped_settings(refeed_max_chars=200), client=fake)

    with pytest.raises(LLMResponseError):
        await client.chat_json("extract", [{"role": "user", "content": "hi"}], max_attempts=3)

    assert len(fake.calls) == 3
    assert len(fake.messages(0)) == 1
    assert len(fake.messages(1)) == 3
    assert len(fake.messages(2)) == 5
    for index in (1, 2):
        refed = [m for m in fake.messages(index) if m["role"] == "assistant"]
        assert len(refed) == index
        for item in refed:
            content = str(item["content"])
            assert content.startswith(broken[:200]), "回灌的必须还是模型刚写的那份的开头"
            assert len(content) < len(broken), "整份重发等于替它把预算再花一遍"


async def test_a_stalled_retry_loop_hits_the_total_deadline() -> None:
    """〔T-206〕总时限越线抛 `LLMError(code="llm_timeout")`，不是 `LLMResponseError`。

    两者的区别是给客户端的结论：`llm_timeout` 说"没等到答案"（502，可以整批重传），
    `llm_response_invalid` 说"等到了但不合格"（重传大概率还是不合格）。
    """
    hanging = _HangingCompletions()
    client = LLMClient(_scoped_settings(retry_deadline_seconds=1), client=hanging)

    with pytest.raises(LLMError) as caught:
        await client.chat_json("extract", [{"role": "user", "content": "hi"}], max_attempts=3)

    assert caught.value.code == "llm_timeout"
    assert caught.value.status_code == 502
    # 卡在第一次尝试上就被掐了：一次都没产出去，所以 token 账是空的
    assert caught.value.details["attempts"] == 1
    assert hanging.calls == 1
    assert client.usage.calls == 0
