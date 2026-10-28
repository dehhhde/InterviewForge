"""异常层级 AppError -> NotFound/Validation/LLM/Parse，带 code 与 http_status〔阶段 2 · §5.6〕"""

from typing import Any


class AppError(Exception):
    """所有业务异常的基类：message 面向用户，details 面向排障。"""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details: dict[str, Any] = details or {}


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ValidationFailedError(AppError):
    """规则校验或 schema 校验不通过，对应 422 而非 500。"""

    status_code = 422
    code = "validation_failed"


class ConflictError(AppError):
    """请求与已有行的自然键撞车（如同 `title + company` 的岗位已存在）。

    409 而不是 422：422 说"这个请求体本身不行"，409 说"这个请求体没问题，但库里已经有
    一行了"——客户端据此分支的动作完全不同（改名字 vs 去重/改走 PUT）。
    """

    status_code = 409
    code = "already_exists"


class PayloadTooLargeError(AppError):
    """上传体超过 `upload.*_bytes` 上限（BACKLOG P0-2）。

    413 而不是 422：422 说"内容不合法"，413 说"这个尺寸的服务端不接受"——
    客户端据此做的第一件事是压缩或换原件，而不是改文件名重试。
    """

    status_code = 413
    code = "upload_too_large"


class ParseError(AppError):
    """简历文件读不出文本，属于请求内容问题。"""

    status_code = 422
    code = "parse_failed"


class LLMError(AppError):
    """上游模型调用失败：网络、超时、限流、配额。对客户端表现为坏网关，故 502。"""

    status_code = 502
    code = "llm_error"


class LLMResponseError(AppError):
    """模型通了但产出不可用（JSON 解析或结构校验重试耗尽）。"""

    status_code = 502
    code = "llm_response_invalid"


class BudgetExceededError(AppError):
    """单次运行 token 用量越过 `agent.max_tokens_per_run`（M6 成本护栏）。

    429 的语义是"当前不接受这次请求"，与"预算烧穿后拒绝再扇出一批题"一致；
    它发生在 `generate` 扇出之前，所以客户端拿到失败时这一轮还没花出题的钱。
    """

    status_code = 429
    code = "cost_budget_exceeded"
