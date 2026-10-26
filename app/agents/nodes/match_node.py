"""岗位匹配节点：先规则对齐，剩下的才问模型〔阶段 7 · §10.4.3〕

节点契约（计划书 §4）在这里的落法：
- 只返回增量：`matched_gaps` + 可选的 `errors`，`prompt_version` 走保留键交给 `traced()`；
- 依赖全部装配期注入（`llm` / `retriever` / `settings`），节点内部不 new client；
- **检索分支只有接口**：`retriever` 默认是 `NullRetriever`，恒召回 0 条，所以 M4 跑的是
  无检索降级版；阶段 6 换成 sqlite-vec 实现时本文件一行都不用改；
- 幂等：`build_match` 是纯函数，回退重跑只会算出同一份缺口清单。

为什么词典判完还要留一次模型调用：JD 里总有一批不带技术名的软要求
（"较强的沟通能力"、"能快速熟悉业务"），正则对它们无解，而这些恰恰是面试要问的。
模型只判**这些**，且它给出的证据必须能在简历原文里逐字找到，否则降级成 `unverified`。
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agents.state import AgentState
from app.agents.workflow import PROMPT_VERSION_KEY, Node
from app.config import Settings, get_settings
from app.exceptions.base import LLMError, LLMResponseError, ValidationFailedError
from app.prompts.registry import get_prompt
from app.services.match_service import (
    Requirement,
    adopt_llm_gaps,
    build_match,
    make_gap,
)
from app.utils.llm_client import LLMClient
from app.vectorstore.retriever import NullRetriever, RetrievedPassage, SkillRetriever

#: 一次 prompt 最多带几条待判定要求。JD 写得越啰嗦软要求越多，不设上限就是拿 token
#: 换重复劳动，而且模型在后面几条上的注意力会明显下降
_MAX_UNJUDGED_PER_CALL = 12


class _GapJudgement(BaseModel):
    requirement: str = Field(min_length=2)
    level: Literal["matched", "partial", "missing"]
    evidence: str | None = None


class _MatchPayload(BaseModel):
    """`match.system.j2` 约定的返回形状。"""

    gaps: list[_GapJudgement] = Field(default_factory=list)


def make_match_node(
    *,
    llm: LLMClient | None = None,
    retriever: SkillRetriever | None = None,
    settings: Settings | None = None,
) -> Node:
    """造 match 节点。词典能判的就一次模型都不花。"""
    settings = settings or (llm.settings if llm is not None else get_settings())
    client = llm or LLMClient(settings)
    retrieval = retriever or NullRetriever()

    async def match_node(state: AgentState) -> dict[str, Any]:
        parsed = state.get("parsed")
        if not isinstance(parsed, dict):
            # 没有抽取结果时无从比对。这不是"质量问题"而是"没输入"：回退预算在 validate
            # 那一头已经花完，此处返回空清单只会让画像替一份不存在的简历编证据
            raise ValidationFailedError(
                "缺少简历抽取结果，无法与岗位要求比对",
                code="match_no_parsed",
                details={"resume_id": state.get("resume_id")},
            )
        jd_text = (state.get("jd_text") or "").strip()
        if not jd_text:
            raise ValidationFailedError(
                "缺少岗位 JD 原文，无法比对",
                code="match_no_jd",
                details={"resume_id": state.get("resume_id")},
            )

        raw_text = state.get("raw_text") or ""
        result = build_match(jd_text, parsed)
        errors = [f"match: {note}" for note in result.notes]
        gaps = list(result.gaps)
        stamp = ""

        if result.unresolved:
            adopted, stamp, llm_errors = await _judge(
                client, retrieval, result.unresolved, parsed, raw_text, settings
            )
            gaps += adopted
            errors += llm_errors
            # 模型没接住的（含整次调用失败）也要留名：它们以 unverified 进清单，
            # 下游把它们当"没依据"，而不是当"JD 没这条要求"
            adopted_texts = {str(gap["requirement"]) for gap in adopted}
            gaps += [
                make_gap(item.text, "unverified", section=item.section, source="unjudged")
                for item in result.unresolved
                if item.text not in adopted_texts
            ]

        update: dict[str, Any] = {"matched_gaps": gaps}
        if errors:
            update["errors"] = errors
        if stamp:
            update[PROMPT_VERSION_KEY] = stamp
        return update

    return match_node


async def _judge(
    client: LLMClient,
    retrieval: SkillRetriever,
    unresolved: Sequence[Requirement],
    parsed: Mapping[str, Any],
    raw_text: str,
    settings: Settings,
) -> tuple[list[dict[str, Any]], str, list[str]]:
    """给词典判不了的要求补一次模型判定。返回（可用缺口，模板标记，说明）。"""
    batch = list(unresolved[:_MAX_UNJUDGED_PER_CALL])
    notes: list[str] = []
    if len(unresolved) > len(batch):
        notes.append(
            f"待判定要求共 {len(unresolved)} 条，本轮只送 {len(batch)} 条进模型，其余按待核处理"
        )

    passages = await _retrieve(retrieval, batch, settings)
    prompt = get_prompt("match")
    try:
        payload = await client.chat_json(
            "extract",
            prompt.render(
                requirements_json=_dump([item.as_dict() for item in batch]),
                skills_json=_dump(parsed.get("skills") or []),
                passages_json=_dump(passages),
            ),
            schema=_MatchPayload,
            temperature=0.0,
        )
    except (LLMError, LLMResponseError) as exc:
        # 模型坏了不拦路：规则层已经判出的缺口照常往下走
        return [], "", [f"match: 软要求的模型判定没跑成（{exc.code}），这些要求按待核处理"]
    adopted, dropped = adopt_llm_gaps(
        (item.model_dump() for item in payload.gaps),
        requirements=batch,
        raw_text=raw_text,
    )
    notes += [f"match: {note}" for note in dropped]
    # 一条都没采纳成功就别留模板标记：trace 里的版本号是给"哪版模板变差了"归因的，
    # 用上的那次才算数
    stamp = f"{prompt.node}@{prompt.version}" if adopted else ""
    return adopted, stamp, notes


async def _retrieve(
    retrieval: SkillRetriever, batch: Sequence[Requirement], settings: Settings
) -> list[dict[str, Any]]:
    """检索分支的现调用点。`NullRetriever` 下恒为空列表，一次向量库都不碰。"""
    out: list[dict[str, Any]] = []
    for item in batch:
        found: Sequence[RetrievedPassage] = await retrieval.retrieve(
            item.text, settings.retrieval.top_k_final
        )
        out.extend(
            {
                "requirement": item.text,
                "text": passage.text,
                "score": round(float(passage.score), 4),
            }
            for passage in found
        )
    return out


def _dump(payload: Any) -> str:
    """`ensure_ascii=False`：中文以中文出现，省 token 也好读。"""
    return json.dumps(payload, ensure_ascii=False, default=str)
