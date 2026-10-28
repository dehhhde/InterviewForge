"""LLM 统一客户端〔阶段 4 · §7.3〕

多模型路由 / tenacity 重试 / 并发限流 / chat_json 校验重试 / 成本统计。

分层铁律：所有 LLM 调用必须经过本模块，节点里不许自己 new client。
两类重试边界不同，别混用：
- 传输重试（tenacity）：连接错误 / 超时 / 限流，同一条 messages 重发。
- 校验重试（chat_json 内部）：模型答非所问，把错误反馈拼回对话再问，次数由
  `validate_max_retries` 约束。

两者相乘就是"一个逻辑调用最多 `transport_max_attempts × 校验轮数` 次 HTTP"，所以另有一条
**总时限**（`llm.retry_deadline_seconds`）给相乘的结果封顶，以及回灌截断
（`llm.refeed_max_chars`）——它们不是第三层重试，只是给最坏情况定价〔T-206〕。
"""

import asyncio
import inspect
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)
from pydantic import BaseModel, ValidationError
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.config import Settings, get_settings
from app.exceptions.base import LLMError, LLMResponseError

#: 一次调用服务于哪个环节，决定用哪个模型。
#: `vision` 不属于图节点，是扫描件解析器（`parsers/image_parser.py`）的读图调用。
Purpose = Literal["extract", "generate", "review", "vision"]

#: `content` 允许是字符串（纯文本）或内容块列表（`{"type": "text"|"image_url", ...}`），
#: 多模态请求需要后者；这里不锁死类型，交给调用方与 OpenAI 兼容端点校验。
Message = Mapping[str, Any]
_TRANSPORT_RETRYABLE = (APIConnectionError, APITimeoutError, RateLimitError)
_JSON_FENCE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.S)


@dataclass(slots=True)
class UsageTotals:
    """按模型累计的 token 用量。"""

    calls: int = 0
    failed_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    per_model: dict[str, dict[str, int]] = field(default_factory=dict)

    def record(self, model: str, prompt: int, completion: int) -> None:
        self.calls += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        bucket = self.per_model.setdefault(model, {"prompt": 0, "completion": 0, "calls": 0})
        bucket["prompt"] += prompt
        bucket["completion"] += completion
        bucket["calls"] += 1


class LLMClient:
    """把 `.env` 的模型路由与 `configs/*.yaml` 的护栏落实成一次 await。

    `client` 仅为测试注入点：传入任何具备
    `chat.completions.create(...)` 协程的对象即可，不必是真正的 AsyncOpenAI。
    """

    def __init__(self, settings: Settings | None = None, client: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self.usage = UsageTotals()
        self._sem = asyncio.Semaphore(self.settings.llm.max_concurrency)
        self._closed = False
        self._client = client or AsyncOpenAI(
            api_key=self.settings.deepseek_api_key.get_secret_value(),
            base_url=self.settings.deepseek_base_url,
            timeout=self.settings.llm.request_timeout_seconds,
        )

    def model_for(self, purpose: Purpose) -> str:
        routing: dict[str, str] = {
            "extract": self.settings.llm_model_extract,
            "generate": self.settings.llm_model_generate,
            "review": self.settings.llm_model_review,
            "vision": self.settings.llm_model_vision,
        }
        return routing[purpose]

    async def chat(
        self,
        purpose: Purpose,
        messages: Sequence[Message],
        *,
        max_tokens: int | None = None,
        temperature: float = 0.2,
        json_mode: bool = False,
    ) -> str:
        """一次补全，返回文本。传输耗尽或上游回 4xx/5xx 时抛 `LLMError`。"""
        model = self.model_for(purpose)
        payload = list(messages)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": payload,
            "max_tokens": max_tokens or self.settings.llm.default_max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp: Any = None
        try:
            # 信号量包住**一次尝试**而不是整个重试循环〔T-207 / BACKLOG P2「信号量粒度」〕。
            # 包在外面的话，指数退避那几秒（最长 ~8s）也占着一个槽：`max_concurrency=4` 时
            # 上游一限流，四个槽全在睡觉，出题的二十几个分支只能干等。
            # 语义变化要想清楚：同一个逻辑调用在不同尝试上各占一次槽，
            # 也就是说"并发上限"约束的是**在途请求数**，而不是"同时进行几个逻辑调用"——
            # 前者才是我们要挡的东西（连接池与上游 QPS），后者只是它的副产品。
            async for attempt in self._retrying():
                with attempt:
                    async with self._sem:
                        resp = await self._client.chat.completions.create(**kwargs)
        except _TRANSPORT_RETRYABLE as exc:
            self.usage.failed_calls += 1
            raise LLMError(
                f"模型 {model} 调用失败：{type(exc).__name__}",
                details={"model": model, "attempts": self.settings.llm.transport_max_attempts},
            ) from exc
        except APIStatusError as exc:
            # 上游明确回了 4xx/5xx（实测 2026-10-20：402 "Insufficient Balance"）。
            # 重发不会变好，所以不进 tenacity；但必须换成 `LLMError`：裸 `APIStatusError`
            # 落到 M6 的任务行里只会记成 `internal_error`，轮询方看不出"是账户没钱了"。
            self.usage.failed_calls += 1
            raise LLMError(
                f"模型 {model} 被上游拒绝：HTTP {exc.status_code}",
                details={"model": model, "status_code": exc.status_code},
            ) from exc
        text = _extract_text(resp)
        self._accumulate(model, resp)
        return text

    def _retrying(self) -> AsyncRetrying:
        llm = self.settings.llm
        return AsyncRetrying(
            retry=retry_if_exception_type(_TRANSPORT_RETRYABLE),
            stop=stop_after_attempt(llm.transport_max_attempts),
            wait=wait_exponential_jitter(multiplier=0.5, max=8, jitter=0.2),
            reraise=True,
        )

    async def chat_json(
        self,
        purpose: Purpose,
        messages: Sequence[Message],
        *,
        schema: type[BaseModel] | None = None,
        max_attempts: int | None = None,
        temperature: float = 0.2,
    ) -> Any:
        """要求模型返回 JSON，并把结构校验失败反馈回去重问。

        返回 `dict`（未给 schema）或 schema 实例。重试用尽仍不合格抛
        `LLMResponseError`——注意它与 `LLMError` 不同：上游是通的，产出不可用。

        整段循环套一条**墙钟总时限**（`llm.retry_deadline_seconds`）〔T-206〕：越线时
        抛 `LLMError`（`code="llm_timeout"`）而不是 `LLMResponseError`——前者说"没等到答案"，
        后者说"等到了但不合格"，客户端看到 `status_code` 之后做的事不一样。
        """
        attempts = (
            max_attempts
            if max_attempts is not None
            else self.settings.agent.validate_max_retries + 1
        )
        deadline = self.settings.llm.retry_deadline_seconds
        refeed_limit = self.settings.llm.refeed_max_chars
        payload = list(messages)
        last_error = "未产出内容"
        rounds = 0
        try:
            async with asyncio.timeout(deadline):
                for _ in range(attempts):
                    rounds += 1
                    raw = await self.chat(purpose, payload, temperature=temperature, json_mode=True)
                    try:
                        data = _parse_json(raw)
                        if schema is None:
                            return data
                        return schema.model_validate(data)
                    except (ValueError, json.JSONDecodeError, ValidationError) as exc:
                        last_error = _brief_error(exc)
                        # 回灌的是**截断后**的原文〔T-206〕：要改的是结构，而结构错误在前几百字
                        # 就看得见，整份重发等于替它把刚写的预算再花一遍
                        hint = f"上次输出不合格：{last_error}。只重新输出修正后的 JSON。"
                        payload = [
                            *payload,
                            {"role": "assistant", "content": _clip(raw, refeed_limit)},
                            {"role": "user", "content": hint},
                        ]
        except TimeoutError as exc:
            raise LLMError(
                f"模型调用超过总时限 {deadline}s（第 {rounds}/{attempts} 轮未完成）",
                code="llm_timeout",
                details={"attempts": rounds},
            ) from exc
        raise LLMResponseError(
            f"模型连续 {attempts} 次未产出合规 JSON",
            details={"attempts": attempts, "last_error": last_error},
        )

    def _accumulate(self, model: str, resp: Any) -> None:
        usage = getattr(resp, "usage", None)
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        self.usage.record(model, prompt, completion)

    async def aclose(self) -> None:
        """关闭底层 `AsyncOpenAI` 的连接池〔T-011 / BACKLOG P1-6〕。

        客户端每请求新建（`Semaphore`/`UsageTotals` 是"一次运行"的量纲），不关就只能靠 GC。
        幂等：重复调用不再触碰底层。测试注入的假件多半没有 `aclose`，有 `close` 就用
        `close`，两者都没有就只记关闭标记。
        """
        if self._closed:
            return
        self._closed = True
        closer = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
        if closer is None:
            return
        result = closer()
        if inspect.isawaitable(result):
            await result


def _extract_text(resp: Any) -> str:
    choices = getattr(resp, "choices", None)
    if not choices:
        raise LLMResponseError("模型返回空 choices")
    content = choices[0].message.content
    return cast(str, content or "")


def _parse_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    fenced = _JSON_FENCE.search(text)
    if fenced:
        text = fenced.group(1)
    loaded = json.loads(text)
    if not isinstance(loaded, dict):
        raise ValueError(f"期望 JSON 对象，得到 {type(loaded).__name__}")
    return loaded


def _clip(text: str, limit: int) -> str:
    """截断回灌给模型的上一轮输出，截断处留一个显式记号〔T-206〕。

    记号不是装饰：不标出来，模型会认为自己上次交的就是完整一份，于是去修结尾的括号，
    而真正缺的东西在它看不见的后半段。
    """
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n……（此处已截断，请按下面的说明重新输出完整 JSON）"


def _brief_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:3])
    return str(exc)[:200]
