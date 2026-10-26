"""LLM 统一客户端〔阶段 4 · §7.3〕

多模型路由 / tenacity 重试 / 并发限流 / chat_json 校验重试 / 成本统计。

分层铁律：所有 LLM 调用必须经过本模块，节点里不许自己 new client。
两类重试边界不同，别混用：
- 传输重试（tenacity）：连接错误 / 超时 / 限流，同一条 messages 重发。
- 校验重试（chat_json 内部）：模型答非所问，把错误反馈拼回对话再问，次数由
  `validate_max_retries` 约束。
"""

import asyncio
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
            async with self._sem:
                async for attempt in self._retrying():
                    with attempt:
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
        """
        attempts = (
            max_attempts
            if max_attempts is not None
            else self.settings.agent.validate_max_retries + 1
        )
        payload = list(messages)
        last_error = "未产出内容"
        for _ in range(attempts):
            raw = await self.chat(purpose, payload, temperature=temperature, json_mode=True)
            try:
                data = _parse_json(raw)
                if schema is None:
                    return data
                return schema.model_validate(data)
            except (ValueError, json.JSONDecodeError, ValidationError) as exc:
                last_error = _brief_error(exc)
                payload = [
                    *payload,
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": f"上次输出不合格：{last_error}。只重新输出修正后的 JSON。",
                    },
                ]
        raise LLMResponseError(
            f"模型连续 {attempts} 次未产出合规 JSON",
            details={"attempts": attempts, "last_error": last_error},
        )

    def _accumulate(self, model: str, resp: Any) -> None:
        usage = getattr(resp, "usage", None)
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        self.usage.record(model, prompt, completion)


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


def _brief_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:3])
    return str(exc)[:200]
