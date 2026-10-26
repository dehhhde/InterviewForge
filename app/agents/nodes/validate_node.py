"""信息校验节点：规则先行，规则全过才让 LLM 反思〔阶段 7 · §10.4.2〕

节点契约在这里的落法：
- 只返回增量：`validate_issues` / `validate_retries` / 修正后的 `parsed`，`errors` 走只追加通道；
- **计数由本节点自增**：判出问题才 +1，`route_after_validate` 用 `<=` 比较，
  `validate_max_retries=2` 因此等于"parse 最多重跑 3 次"；
- 不改写 `raw_text`：原始抽取结果留在 parse 那一步，回放时看的是它，不是修过的版本；
- LLM 的意见只有点名了已知字段才算数，否则当没说过。

为什么要给 LLM 的输出加这道结构闸：parse 是确定性的，同一份简历重跑必然给出同一个
`parsed`，所以一条凭感觉的问题会把重试预算一路烧光还修不掉任何东西。闸门的代价是
漏掉不点名的批评，而那些批评本来也没法被抽取节点执行。
"""

import json
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from app.agents.state import AgentState
from app.agents.workflow import PROMPT_VERSION_KEY, Node
from app.config import Settings, get_settings
from app.exceptions.base import LLMError, LLMResponseError
from app.prompts.registry import get_prompt
from app.schemas.resume import ResumeSchema
from app.services.resume_service import validate_resume
from app.utils.llm_client import LLMClient

#: LLM 只许对这些字段提意见；名字来自 `ResumeSchema`，加了字段不用记得改这里
_KNOWN_FIELDS = frozenset(ResumeSchema.model_fields)


class _ReflectPayload(BaseModel):
    """`validate.system.j2` 约定的返回形状。"""

    issues: list[str] = Field(default_factory=list)


def make_validate_node(
    *, llm: LLMClient | None = None, settings: Settings | None = None
) -> Node:
    """造 validate 节点。LLM 只在规则全过的时候才被叫起来，正常简历一次调用都不多花。"""
    settings = settings or (llm.settings if llm is not None else get_settings())
    client = llm or LLMClient(settings)

    async def validate_node(state: AgentState) -> dict[str, Any]:
        raw_text = state.get("raw_text") or ""
        parsed = state.get("parsed")
        update: dict[str, Any] = {}

        issues: list[str] = []
        if parsed is None:
            # 没有抽取结果时无从校验，也不必读模板——把话留给 parse
            issues.append("还没有抽取结果，校验无从下手")
        else:
            outcome = validate_resume(raw_text, parsed)
            issues = list(outcome.issues)
            update["parsed"] = outcome.parsed
            update["errors"] = [f"validate: {repair}" for repair in outcome.repairs]
            # 规则还挂着就别花 LLM 的钱：让 parse 重跑之后自然还会再校验一次
            if not issues:
                llm_issues, stamp = await _reflect(client, raw_text, outcome.parsed)
                issues = llm_issues
                if stamp:
                    update[PROMPT_VERSION_KEY] = stamp

        update["validate_issues"] = issues
        if issues:
            update["validate_retries"] = state.get("validate_retries", 0) + 1
        return update

    return validate_node


async def _reflect(
    client: LLMClient, raw_text: str, parsed: dict[str, Any]
) -> tuple[list[str], str]:
    """让模型挑"抽取错位"。返回（闸门放行后的问题，用到的模板版本）。

    `today` 必须传：真调时模型把 `2026-06-30` 离职判成"未来日期"，因为它拿自己的
    知识截止日当今天。时间判断只能由外部给基准。
    """
    prompt = get_prompt("validate")
    try:
        payload = await client.chat_json(
            "extract",
            prompt.render(
                raw_text=raw_text, parsed_json=_dump(parsed), today=date.today().isoformat()
            ),
            schema=_ReflectPayload,
            temperature=0.0,
        )
    except (LLMError, LLMResponseError):
        # 反思环节坏了不该拦住流程，更不该让 parse 白跑：规则已经点头了
        return [], ""
    return (
        [issue for issue in payload.issues if _names_known_field(issue)],
        f"{prompt.node}@{prompt.version}",
    )


def _dump(parsed: dict[str, Any]) -> str:
    """喂给模型的抽取结果。`ensure_ascii=False` 让中文仍以中文出现，省 token 也好读。"""
    return json.dumps(parsed, ensure_ascii=False, default=str)


def _names_known_field(issue: str) -> bool:
    return any(name in issue for name in _KNOWN_FIELDS)
