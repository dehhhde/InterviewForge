"""`configure_logging()`：`settings.log_level` 是否真的决定输〔T-017 / BACKLOG P3-1〕

这一组用例碰的是**进程级全局状态**（`logging` 模块的 logger 表），所以每条都套
`restore_app_logging`：跑完把 `app` 与被牵连的那个外部 logger 恢复原样。
不恢复的话，本文件里第一条用例设的 DEBUG 会一路带到会话结束，后面任何挂在 root 上
的 handler 都会开始收 `app.*` 的调试记录——表现为"单独跑绿、整跑冒一堆日志"。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.utils.logging import configure_logging

#: 一个**不在**本次配置名单里的 logger，用它验 `disable_existing_loggers=False`
#: 真的护住了 uvicorn 那三个 logger。
_FOREIGN = "uvicorn.access"


def _snapshot(name: str) -> tuple[int, list[logging.Handler], bool, bool]:
    logger = logging.getLogger(name)
    return (logger.level, list(logger.handlers), logger.propagate, logger.disabled)


def _restore(name: str, state: tuple[int, list[logging.Handler], bool, bool]) -> None:
    logger = logging.getLogger(name)
    logger.level, logger.handlers[:], logger.propagate, logger.disabled = state


@pytest.fixture
def restore_logging_state() -> Iterator[None]:
    app_state = _snapshot("app")
    foreign_state = _snapshot(_FOREIGN)
    yield
    _restore("app", app_state)
    _restore(_FOREIGN, foreign_state)


def _app_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name.startswith("app.")]


def test_the_configured_level_lands_on_the_app_logger(restore_logging_state: None) -> None:
    configure_logging("DEBUG")
    assert logging.getLogger("app").level == logging.DEBUG


def test_level_defaults_to_the_setting(
    restore_logging_state: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 不传参走的是 `get_settings()`，而它是 lru_cache 单例（AGENTS.md §6）→ 改属性，不改 .env
    monkeypatch.setattr(get_settings(), "log_level", "ERROR")
    configure_logging()
    assert logging.getLogger("app").level == logging.ERROR


def test_a_record_above_the_level_reaches_caplog(
    restore_logging_state: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_logging("DEBUG")
    logging.getLogger("app.services.task").debug("一条调试记录")
    assert [r.getMessage() for r in _app_records(caplog)] == ["一条调试记录"]


def test_records_below_the_level_are_dropped(
    restore_logging_state: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """这条才是"log_level 真生效"的正证：只验 DEBUG 收得到，WARNING 和 DEBUG 都算过。"""
    configure_logging("WARNING")
    logging.getLogger("app.services.task").info("低于阈值，应当落不了地")
    assert _app_records(caplog) == []
    logging.getLogger("app.services.task").error("到了阈值")
    assert [r.name for r in _app_records(caplog)] == ["app.services.task"]


def test_loggers_outside_the_config_stay_enabled(restore_logging_state: None) -> None:
    """`disable_existing_loggers` 一旦写回默认 `True`，uvicorn 的访问日志会**静默消失**。

    dictConfig 对没点名的既有 logger 做的事就是"关掉"，而且不报错，所以这里显式断言
    那个 logger 还活着。
    """
    configure_logging("DEBUG")
    assert logging.getLogger(_FOREIGN).disabled is False


def test_configure_does_not_take_over_the_root_logger(restore_logging_state: None) -> None:
    """抢 root 会让每条日志打两遍：我们的 handler 一次，uvicorn 挂在 root 上的再一次。"""
    root_handlers = list(logging.getLogger("").handlers)
    configure_logging("DEBUG")
    assert logging.getLogger("").handlers == root_handlers


def test_configuring_twice_does_not_stack_handlers(restore_logging_state: None) -> None:
    """lifespan 每个用例新建一次 app，重复装配是常态；会累加就是每条日志打 N 遍。"""
    configure_logging("INFO")
    first = len(logging.getLogger("app").handlers)
    configure_logging("INFO")
    assert len(logging.getLogger("app").handlers) == first


def test_the_lifespan_configures_logging_on_startup(restore_logging_state: None) -> None:
    """接线的证据必须来自 `create_app()` 的生命周期，不是"函数存在"。

    `tests/test_api/` 用 `ASGITransport`，它**不跑 lifespan**，所以这里用 `TestClient`
    的上下文管理器——它的 `__enter__/__exit__` 就是 startup/shutdown。
    """
    logging.getLogger("app").handlers.clear()
    with TestClient(create_app()):
        assert logging.getLogger("app").handlers != []
