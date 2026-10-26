"""通用装饰器：`@log_call`（贯穿 request_id）与 `@retry`〔阶段 1 · §4.3〕

`request_id_var` 定义在 utils 层而非 middleware 层：agent 节点、BackgroundTasks 这些
非 HTTP 入口也要能自己造一个 id，不能反向依赖 web 层。阶段 2 的请求中间件负责 set。
"""

import functools
import inspect
import logging
import time
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any, TypeVar, cast

from tenacity import (
    AsyncRetrying,
    Retrying,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

logger = logging.getLogger("app.call")

#: 一次调用链的关联 id；未进入请求上下文时为 "-"。
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

F = TypeVar("F", bound=Callable[..., Any])

# 入参常含整页简历原文，日志只留前若干字符，其余用长度代替
_ARG_REPR_LIMIT = 120


def _brief(value: Any) -> str:
    text = repr(value)
    if len(text) <= _ARG_REPR_LIMIT:
        return text
    return f"{text[:_ARG_REPR_LIMIT]}…(共 {len(text)} 字符)"


def log_call[F: Callable[..., Any]](func: F) -> F:
    """给一次调用打 start / ok / fail 三段日志，附带耗时。异常原样抛出，不吞、不改。"""

    def _log_start(args: tuple[Any, ...], kwargs: dict[str, Any]) -> float:
        logger.info(
            "%s 开始 rid=%s 入参=%s",
            func.__qualname__,
            request_id_var.get(),
            _brief((args, kwargs)),
        )
        return time.perf_counter()

    def _log_ok(started: float) -> None:
        logger.info(
            "%s 完成 rid=%s 耗时=%.1fms",
            func.__qualname__,
            request_id_var.get(),
            (time.perf_counter() - started) * 1000,
        )

    def _log_fail(started: float) -> None:
        logger.exception(
            "%s 失败 rid=%s 耗时=%.1fms",
            func.__qualname__,
            request_id_var.get(),
            (time.perf_counter() - started) * 1000,
        )

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            started = _log_start(args, kwargs)
            try:
                result = await func(*args, **kwargs)
            except Exception:
                _log_fail(started)
                raise
            _log_ok(started)
            return result

        return cast("F", async_wrapper)

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        started = _log_start(args, kwargs)
        try:
            result = func(*args, **kwargs)
        except Exception:
            _log_fail(started)
            raise
        _log_ok(started)
        return result

    return cast("F", sync_wrapper)


def retry(
    *,
    max_attempts: int = 3,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[F], F]:
    """指数退避重试，耗尽后原样抛出最后一次异常。

    `retry_on` 默认放行所有 `Exception`，但调用方应按实际失败模式收窄——
    把校验错误也重试三遍，只会让脏数据看起来"偶尔能过"。
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts 至少 1 次，当前 {max_attempts}")

    def decorator(func: F) -> F:
        options: dict[str, Any] = {
            "stop": stop_after_attempt(max_attempts),
            "wait": wait_exponential_jitter(multiplier=0.2, max=2.0, jitter=0.1),
            "retry": retry_if_exception_type(retry_on),
            "before_sleep": before_sleep_log(logger, logging.WARNING),
            "reraise": True,
        }

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                async for attempt in AsyncRetrying(**options):
                    with attempt:
                        return await func(*args, **kwargs)
                raise AssertionError("tenacity 耗尽后必定抛出，不会走到这里")

            return cast("F", async_wrapper)

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            for attempt in Retrying(**options):
                with attempt:
                    return func(*args, **kwargs)
            raise AssertionError("tenacity 耗尽后必定抛出，不会走到这里")

        return cast("F", sync_wrapper)

    return decorator
