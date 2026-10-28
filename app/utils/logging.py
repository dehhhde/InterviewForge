"""日志装配：让 `settings.log_level` 真的决定输出〔T-017 / BACKLOG P3-1〕

在这个文件出现之前，`log_level` 是纯装饰——全仓没有任何 `dictConfig` / `basicConfig` /
`setLevel` 调用点，`app.*` 那些 logger 只能靠解释器的 lastResort 兜底，而它只放行
**WARNING 以上**。所以 `configs/development.yaml` 写着 `DEBUG` 却一条日志都不落，
读代码的人以为"没日志 = 这个模块没打日志"，实际是配置根本没生效。

两条形状上的约束，都是为了不踩 uvicorn：

- **只配 `app` 这一个 logger，不碰 root**。uvicorn 自己已经给 `uvicorn` / `uvicorn.error` /
  `uvicorn.access` 三个 logger 挂好了 handler，我们去抢 root 只会让每条日志打两遍。
  `app.*` 的子 logger（`app.services.task`、`app.tasks.question_generation`……）靠命名层级
  继承这一个配置，不需要逐个点名。
- **`disable_existing_loggers` 必须是 `False`**。`dictConfig` 的默认值是 `True`，含义是
  "把这次配置里没点名的既有 logger 全部关掉"——而 lifespan 跑在 uvicorn 装配日志**之后**，
  默认值会顺手把 uvicorn 的三个 logger 静音，访问日志就此消失，且没有任何报错。
"""

from __future__ import annotations

import logging
import logging.config
from typing import Any

from app.config import LogLevel, get_settings

#: 唯一被点名的 logger。所有 `app.` 前缀的记录都从这里继承级别与 handler。
_ROOT_APP_LOGGER = "app"

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def _config_for(level: str) -> dict[str, Any]:
    """把"级别"翻成一份 dictConfig。单独拆出来，好让用例断言形状而不必先装一遍。"""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"app": {"format": _FORMAT, "datefmt": "%H:%M:%S"}},
        "handlers": {
            # 走 stdout：`start.sh` 里 uvicorn 的日志也进终端，两套混在一个流里才好用
            # `less` / 重定向看；分两个流只会让人去猜哪边少了什么。
            "console": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "app",
            },
        },
        # `propagate` 留着 True：root 上没有我们的 handler（uvicorn 不配 root），所以生产
        # 不会重复输出；而 pytest 的 `caplog` 正是挂在 root 上抓记录的。
        "loggers": {"app": {"level": level, "handlers": ["console"], "propagate": True}},
    }


def configure_logging(level: LogLevel | None = None) -> None:
    """按配置装配 `app.*` 的日志。

    `level` 省略时取 `settings.log_level`——生产只该由 lifespan 无参调用一次；
    显式传值是给用例留的口子，免得测试为了换级别去 monkeypatch `lru_cache` 单例。
    重复调用是安全的：`dictConfig` 会先卸掉本配置里那些 logger 的旧 handler 再装新的，
    所以不会越装越多。
    """
    logging.config.dictConfig(_config_for(level or get_settings().log_level))
