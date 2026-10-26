"""全局异常处理器，统一响应体且不外泄堆栈〔阶段 2 · §5.6，M6 落地〕

`AppError` → `JSONResponse(status_code, {"error": {code, message, details}})`。
三条边界：

- **只暴露我们自己的异常类型**。未预期的异常一律 500 + 固定文案，堆栈只进日志——
  响应体里带 `repr(exc)` 等于把依赖版本、路径结构白送给客户端。
- **`details` 由抛出方构造**（见 `AppError` 的约定：面向排障），里面不该有密钥；
  这条约定在 `AGENTS.md` 里，处理器不再兜第二遍。
- 422 的 `RequestValidationError` 也收进来：FastAPI 默认体会把输入回显原样塞进
  `ctx`，简历原文作为表单字段时那就是一次数据外泄面。这里只留 `loc` 与 `msg`。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.exceptions.base import AppError

logger = logging.getLogger("app.exceptions")


def register_exception_handlers(app: FastAPI) -> None:
    """把三类异常挂到 app 上。`Exception` 处理器要求 Starlette 的 catch-all 语义。"""

    @app.exception_handler(AppError)
    async def _app_error(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {"code": exc.code, "message": exc.message, "details": exc.details}
            },
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        problems = [
            {"loc": [str(part) for part in err.get("loc", [])], "msg": str(err.get("msg", ""))}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_failed",
                    "message": "请求体校验不通过",
                    "details": {"problems": problems},
                }
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        # 完整异常只进日志；响应体里连类型名都不给，避免泄漏内部结构。
        logger.exception("未处理异常", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "服务内部错误，请带时间联系维护者",
                    "details": {},
                }
            },
        )
