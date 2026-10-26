"""候选人画像节点：分数由 rubric 定，模型只写被闸门过滤的叙述〔阶段 7 · §10.4.4〕

节点契约（计划书 §4）在这里的落法：
- 只返回增量：`profile` + 可选 `errors`，`prompt_version` 走保留键交给 `traced()`；
- 幂等：分数与主题清单是纯函数；模型那一环坏了有规则版叙述兜底，重跑结果一致；
- 不直连数据库：`parsed` / `matched_gaps` 都从上游通道读。

分工的理由：`plan` 要确定性，而它读的就是这里的 `dimensions[*].score`——**分数不能来自
模型**（模型给分每年都在漂）。模型的力气因此放在"这堆数字意味着什么"上：措辞可以换，
但每句话都必须点名一个已知的维度名或技能名，否则当没说过。凭空冒出来的"沟通能力强"
既无法核对，还会让下游给一个简历里没提过的东西出题。
"""

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, Field

from app.agents.state import AgentState
from app.agents.workflow import PROMPT_VERSION_KEY, Node
from app.config import Settings, get_settings
from app.exceptions.base import LLMError, LLMResponseError, ValidationFailedError
from app.extractors.base import collapse
from app.prompts.registry import get_prompt
from app.services.profile_service import (
    RUBRIC_VERSION,
    Dimension,
    confidence_from_signals,
    derive_focus_topics,
    fallback_narrative,
    score_dimensions,
)
from app.utils.llm_client import LLMClient

#: 规则版叙述超过这个长度就是在写小作文，出题环节用不上，直接回退
_MAX_SUMMARY_CHARS = 200
#: 短于此的"实体"（"的"、"了"）不配当作点名的依据
_MIN_ENTITY_CHARS = 2


class _NarrativePayload(BaseModel):
    """`profile.system.j2` 约定的返回形状。分数不在其中——画像的分由 rubric 给。"""

    summary: str = ""
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0, le=1)


def make_profile_node(
    *, llm: LLMClient | None = None, settings: Settings | None = None
) -> Node:
    """造 profile 节点。模型只补叙述，分数一律由 `profile_service` 算。"""
    settings = settings or (llm.settings if llm is not None else get_settings())
    client = llm or LLMClient(settings)

    async def profile_node(state: AgentState) -> dict[str, Any]:
        parsed = state.get("parsed")
        if not isinstance(parsed, dict):
            raise ValidationFailedError(
                "缺少简历抽取结果，画像无从打分",
                code="profile_no_parsed",
                details={"resume_id": state.get("resume_id")},
            )

        errors: list[str] = []
        gaps = _gap_list(state.get("matched_gaps"))
        if not gaps:
            # match 被跳过或整份 JD 没拆出要求：不拦住，但那一维会按中档记，得留话
            errors.append("没有缺口清单可读，岗位匹配度按无依据中档记")

        dimensions = score_dimensions(parsed, gaps)
        topics = derive_focus_topics(parsed, gaps, dimensions)
        confidence = confidence_from_signals(parsed, gaps)
        narrative, stamp, more_errors = await _narrate(
            client, parsed, gaps, dimensions, topics, confidence
        )
        errors += more_errors

        profile: dict[str, Any] = {
            "rubric_version": RUBRIC_VERSION,
            "candidate": {
                "name": str(parsed.get("name") or ""),
                "years_experience": parsed.get("years_experience"),
            },
            "dimensions": [dimension.as_dict() for dimension in dimensions],
            "focus_topics": topics,
            **narrative,
        }
        update: dict[str, Any] = {"profile": profile}
        if errors:
            update["errors"] = [f"profile: {note}" for note in errors]
        if stamp:
            update[PROMPT_VERSION_KEY] = stamp
        return update

    return profile_node


def _gap_list(value: Any) -> list[Mapping[str, Any]]:
    """读 `matched_gaps` 通道。形状不对就当没有，别拿脏数据算命中率。"""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


async def _narrate(
    client: LLMClient,
    parsed: Mapping[str, Any],
    gaps: Sequence[Mapping[str, Any]],
    dimensions: Sequence[Dimension],
    topics: Sequence[Mapping[str, Any]],
    confidence: float,
) -> tuple[dict[str, Any], str, list[str]]:
    """要一段模型叙述。返回（写进 profile 的叙述字段，模板标记，说明）。

    模板用上了就留版本号——哪怕这一版只产出被闸门挡掉的句子，"哪版模板变差了"
    也要能归因。
    """
    prompt = get_prompt("profile")
    fallback = fallback_narrative(parsed, dimensions, topics, confidence)
    try:
        payload = await client.chat_json(
            "extract",
            prompt.render(
                parsed_json=_dump(parsed),
                matched_gaps_json=_dump(list(gaps)),
                rubric_json=_dump(
                    {
                        "dimensions": [dimension.as_dict() for dimension in dimensions],
                        "focus_topics": list(topics),
                        "confidence": confidence,
                    }
                ),
            ),
            schema=_NarrativePayload,
            temperature=0.2,
        )
    except (LLMError, LLMResponseError) as exc:
        return fallback, "", [f"模型叙述这一环没跑成（{exc.code}），改用规则版叙述"]

    entities = _entity_names(parsed, dimensions, topics)
    notes: list[str] = []
    strengths = _gate(payload.strengths, entities, "优势", notes)
    weaknesses = _gate(payload.weaknesses, entities, "劣势", notes)
    summary = collapse(payload.summary)
    if not 2 <= len(summary) <= _MAX_SUMMARY_CHARS:
        notes.append(f"模型给的概述长度 {len(summary)} 不合格，改用规则版")
        summary = str(fallback["summary"])

    # 模型自评的置信度只当上限用：它普遍偏高，而规则版每一项都对应"有几分材料撑着"
    model_confidence = float(payload.confidence)
    final_confidence = (
        round(min(confidence, model_confidence), 2) if model_confidence > 0 else confidence
    )
    used_llm = bool(strengths or weaknesses) or summary != str(fallback["summary"])
    narrative: dict[str, Any] = {
        "summary": summary,
        "strengths": strengths or list(fallback["strengths"]),
        "weaknesses": weaknesses or list(fallback["weaknesses"]),
        "confidence": final_confidence,
        "narrative_source": "llm" if used_llm else "rubric",
    }
    return narrative, f"{prompt.node}@{prompt.version}", notes


def _gate(values: Sequence[str], entities: set[str], label: str, notes: list[str]) -> list[str]:
    """只放行点名了已知实体的句子。"""
    accepted = [collapse(item) for item in values if _names_entity(item, entities)]
    dropped = len(values) - len(accepted)
    if dropped:
        notes.append(f"{label}里有 {dropped} 条没点出任何已知维度或技能，已丢弃")
    return [item for item in accepted if item]


def _entity_names(
    parsed: Mapping[str, Any],
    dimensions: Sequence[Dimension],
    topics: Sequence[Mapping[str, Any]],
) -> set[str]:
    """闸门认得的实体：技能名、项目名、维度名、主题名。全是输入里已有的东西。"""
    out = {dimension.label for dimension in dimensions}
    out |= {str(topic.get("topic") or "") for topic in topics}
    for key in ("skills", "projects"):
        items = parsed.get(key)
        if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
            out |= {str(item.get("name") or "") for item in items if isinstance(item, Mapping)}
    return {name for name in (collapse(item) for item in out) if len(name) >= _MIN_ENTITY_CHARS}


def _names_entity(sentence: str, entities: Iterable[str]) -> bool:
    text = collapse(sentence)
    return any(entity in text for entity in entities)


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)
