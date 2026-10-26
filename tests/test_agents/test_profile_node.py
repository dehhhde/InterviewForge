"""profile 节点的接线测试：分数只来自 rubric、模型叙述要过点名闸（全程离线）。

锁的行为：模型说什么都改不了五维分数与主题清单；它写的句子必须点名一个已知维度或技能
才允许进 `profile`；它挂了就换规则版叙述，画像不能变成空壳。断言不落模型文案。
"""

import json
from collections.abc import Sequence
from typing import Any

import pytest

from app.agents.nodes.profile_node import make_profile_node
from app.agents.workflow import PROMPT_VERSION_KEY
from app.config import get_settings
from app.exceptions.base import LLMError, LLMResponseError, ValidationFailedError
from app.services.profile_service import (
    LABELS,
    RUBRIC_VERSION,
    confidence_from_signals,
    fallback_narrative,
    score_dimensions,
)
from app.utils.llm_client import LLMClient

from ._fake_llm import FakeCompletions, prompt_block

_PARSED: dict[str, Any] = {
    "name": "陈昊",
    "years_experience": 3.0,
    "skills": [
        {"name": "Java", "level": "proficient", "evidence": "Java 精通"},
        {"name": "MySQL", "level": "familiar", "evidence": "熟悉 MySQL 索引原理"},
    ],
    "projects": [{"name": "订单中心", "highlights": ["日峰 2k 单"]}],
}

_GAPS: list[dict[str, Any]] = [
    {
        "requirement": "精通 Java",
        "level": "matched",
        "evidence": "Java 精通",
        "skill": "Java",
        "required_level": "proficient",
        "resume_level": "proficient",
        "section": "required",
        "source": "lexicon",
    },
    {
        "requirement": "掌握 Redis 持久化",
        "level": "missing",
        "evidence": None,
        "skill": "Redis",
        "required_level": None,
        "resume_level": None,
        "section": "required",
        "source": "lexicon",
    },
    {
        "requirement": "熟悉 Kafka 事务",
        "level": "partial",
        "evidence": "熟悉 MySQL 索引原理",
        "skill": "Kafka",
        "required_level": "familiar",
        "resume_level": None,
        "section": "preferred",
        "source": "lexicon",
    },
]

#: 每句都点名了一个已知实体（技能名或维度标签），因此闸门应当全放行
_NAMED = (
    '{"summary": "三年后端，Java 够硬，Redis 完全没碰过。",'
    ' "strengths": ["Java：自称精通，可现场深挖", "技能广度：列出 2 项技能"],'
    ' "weaknesses": ["Redis：JD 要求但简历无证据"],'
    ' "confidence": 0.8}'
)
#: 一句实体都没点名：闸门应当一句都不放
_VAGUE = (
    '{"summary": "整体素质不错，读起来挺顺。",'
    ' "strengths": ["沟通表达能力强"],'
    ' "weaknesses": ["团队协作意识有待提升"],'
    ' "confidence": 0.99}'
)


def _node(
    replies: Sequence[str] = (_NAMED,), raises: Exception | None = None
) -> tuple[Any, FakeCompletions]:
    fake = FakeCompletions(replies, raises)
    return make_profile_node(llm=LLMClient(settings=get_settings(), client=fake)), fake


async def _run(node: Any, **overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {"parsed": _PARSED, "matched_gaps": list(_GAPS)}
    state.update(overrides)
    return await node(state)


def _dims(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in profile["dimensions"]}


async def test_the_model_cannot_move_a_single_score() -> None:
    """M4 的确定性根基：`plan` 读的就是这里的分数，模型说什么都不许它漂。"""
    named_node, _ = _node()
    vague_node, _ = _node((_VAGUE,))

    named = await _run(named_node)
    vague = await _run(vague_node)

    assert named["profile"]["dimensions"] == vague["profile"]["dimensions"]
    assert named["profile"]["focus_topics"] == vague["profile"]["focus_topics"]


async def test_every_dimension_is_scored_one_to_five_and_carries_evidence() -> None:
    """M4 验收：按 rubric 给每个维度 1–5 分 + 证据句。"""
    node, _ = _node()

    profile = (await _run(node))["profile"]

    assert [item["name"] for item in profile["dimensions"]] == list(LABELS)
    for item in profile["dimensions"]:
        assert 1 <= item["score"] <= 5
        assert item["evidence"].strip()


async def test_profile_payload_shape_and_provenance() -> None:
    node, _ = _node()

    profile = (await _run(node))["profile"]

    assert set(profile) == {
        "rubric_version",
        "candidate",
        "dimensions",
        "focus_topics",
        "summary",
        "strengths",
        "weaknesses",
        "confidence",
        "narrative_source",
    }
    assert profile["rubric_version"] == RUBRIC_VERSION
    assert profile["candidate"] == {"name": "陈昊", "years_experience": 3.0}


async def test_sentences_that_name_a_known_entity_get_through_the_gate() -> None:
    node, _ = _node()

    profile = (await _run(node))["profile"]

    assert profile["narrative_source"] == "llm"
    assert profile["strengths"] == ["Java：自称精通，可现场深挖", "技能广度：列出 2 项技能"]
    assert profile["weaknesses"] == ["Redis：JD 要求但简历无证据"]


async def test_prompt_version_is_left_even_when_only_the_summary_survived() -> None:
    """模板用上了就留版本号，哪怕产出被闸门挡掉大半——归因看的是"有没有用这版模板"。"""
    node, _ = _node((_VAGUE,))

    update = await _run(node)

    assert update[PROMPT_VERSION_KEY] == "profile@v1"


async def test_unnamed_sentences_are_dropped_and_the_rubric_ones_take_their_place() -> None:
    """没点名的句子既进不了画像，也不许留下"画像里有个没人提过的优点"。"""
    node, _ = _node((_VAGUE,))

    update = await _run(node)
    profile = update["profile"]
    expected = fallback_narrative(
        _PARSED,
        score_dimensions(_PARSED, _GAPS),
        profile["focus_topics"],
        confidence_from_signals(_PARSED, _GAPS),
    )

    assert profile["strengths"] == list(expected["strengths"])
    assert profile["weaknesses"] == list(expected["weaknesses"])
    assert all(
        any(name in line for name in _entity_candidates(profile)) for line in profile["strengths"]
    ), "进了画像的优势句必须点得出一个已知实体"
    assert any("已丢弃" in note for note in update["errors"])


async def test_confidence_never_exceeds_the_rule_version() -> None:
    """模型自评普遍偏高，规则版才算得出"有几分材料撑着"，所以取小者。"""
    rule = confidence_from_signals(_PARSED, _GAPS)
    high, _ = _node((_VAGUE,))  # 0.99
    low, _ = _node()  # 0.8

    overconfident = (await _run(high))["profile"]
    modest = (await _run(low))["profile"]

    assert overconfident["confidence"] == rule
    assert modest["confidence"] == round(min(rule, 0.8), 2)
    assert modest["confidence"] <= overconfident["confidence"]


async def test_model_confidence_of_zero_leaves_the_rule_value_alone() -> None:
    """模型没给置信度（0）时不许把画像拽成 0 分可信。"""
    reply = json.dumps(
        {"summary": "Java 够硬。", "strengths": [], "weaknesses": [], "confidence": 0},
        ensure_ascii=False,
    )
    node, _ = _node((reply,))

    profile = (await _run(node))["profile"]

    assert profile["confidence"] == confidence_from_signals(_PARSED, _GAPS)


@pytest.mark.parametrize(
    "raises",
    [
        LLMError("上游没响应", code="llm_timeout"),
        LLMResponseError("连续不合格", details={"attempts": 2}),
    ],
    ids=["llm_error", "bad_json"],
)
async def test_model_failure_falls_back_to_the_rubric_narrative(raises: Exception) -> None:
    node, _ = _node(raises=raises)

    update = await _run(node)
    profile = update["profile"]
    dimensions = score_dimensions(_PARSED, _GAPS)
    topics = profile["focus_topics"]

    assert profile["narrative_source"] == "rubric"
    assert profile["summary"] == fallback_narrative(
        _PARSED, dimensions, topics, confidence_from_signals(_PARSED, _GAPS)
    )["summary"]
    assert PROMPT_VERSION_KEY not in update, "没用到模板就不许留版本号"
    assert any("改用规则版叙述" in note for note in update["errors"])


async def test_overlong_summary_is_replaced_but_scores_stay_put() -> None:
    """概述超过 200 字就是在写小作文，出题用不上；但分数不受这条闸门影响。"""
    reply = json.dumps(
        {
            "summary": "Java 与 MySQL 之外还有很多可说的话，够写一整页了" * 10,
            "strengths": ["Java：可深挖"],
            "weaknesses": [],
            "confidence": 0.7,
        },
        ensure_ascii=False,
    )
    node, _ = _node((reply,))

    update = await _run(node)

    assert any("长度" in note for note in update["errors"])
    assert len(update["profile"]["summary"]) <= 200


async def test_missing_parsed_raises_instead_of_profiling_an_empty_resume() -> None:
    node, fake = _node()

    with pytest.raises(ValidationFailedError) as caught:
        await node({})

    assert caught.value.code == "profile_no_parsed"
    assert fake.calls == []


async def test_absent_gap_channel_is_noted_and_scores_job_fit_at_midpoint() -> None:
    """match 被跳过不是错误，但那一维没有依据，得按中档记并留话。"""
    node, _ = _node()

    update = await _run(node, matched_gaps=[])

    assert _dims(update["profile"])["job_fit"]["score"] == 3
    assert any("岗位匹配度按无依据中档记" in note for note in update["errors"])


async def test_dirty_gap_channel_is_tolerated() -> None:
    node, _ = _node()

    update = await _run(node, matched_gaps=["一句不是 dict 的东西", {"level": "missing"}])

    dimensions = update["profile"]["dimensions"]
    assert len(dimensions) == len(LABELS)
    assert all(1 <= item["score"] <= 5 for item in dimensions)


async def test_prompt_carries_the_rubric_result_so_the_model_can_name_things() -> None:
    node, fake = _node()

    await _run(node)

    rubric = prompt_block(fake.user_text, "已算好的 rubric 结果")
    assert {item["name"] for item in rubric["dimensions"]} == set(LABELS)
    assert rubric["focus_topics"]
    gaps_block = prompt_block(fake.user_text, "JD 缺口清单")
    assert {item["skill"] for item in gaps_block} == {"Java", "Redis", "Kafka"}


async def test_rerunning_yields_the_same_profile_when_the_model_stops_talking() -> None:
    """幂等：回退会让本节点重跑，规则部分不能因为跑过一遍就变。"""
    node, _ = _node(raises=LLMError("挂了", code="llm_timeout"))

    first = await _run(node)
    second = await _run(node)

    assert first["profile"] == second["profile"]


async def test_update_only_touches_declared_channels() -> None:
    node, _ = _node()

    update = await _run(node)

    assert set(update) <= {"profile", "errors", PROMPT_VERSION_KEY}
    assert all(note.startswith("profile: ") for note in update.get("errors", []))


def _entity_candidates(profile: dict[str, Any]) -> list[str]:
    """闸门认得的实体集合，测试侧独立算一遍（不复用节点私有函数）。"""
    names = list(LABELS.values())
    names += [str(topic.get("topic") or "") for topic in profile["focus_topics"]]
    names += [str(item.get("name") or "") for item in _PARSED["skills"]]
    names += [str(item.get("name") or "") for item in _PARSED["projects"]]
    return [name for name in names if len(name) >= 2]
