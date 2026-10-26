"""rubric 打分层的单测：分数只由输入决定，主题清单只按弱项排序（全程离线）。

这里刻意**不断言具体分数对应的数字边界**以外的东西——阈值表是会被调的（`_BREADTH_STEPS`
那几张表就是为改阈值而存在的），锁死"15 项技能必然 5 分"这类边界会让调参变成改测试。
锁的是次序与形状：单调、不越界、弱项在前、每条都有依据句。
"""

from typing import Any

from app.services.profile_service import (
    LABELS,
    MAX_FOCUS_TOPICS,
    WEAK_SCORE,
    Dimension,
    confidence_from_signals,
    derive_focus_topics,
    fallback_narrative,
    score_dimensions,
)

_RANKED_SKILLS = [
    {"name": "Java", "level": "proficient", "evidence": "Java 精通"},
    {"name": "Spring Boot", "level": "proficient", "evidence": "Spring Boot 精通"},
    {"name": "MySQL", "level": "familiar", "evidence": "MySQL 熟悉"},
]


def _parsed(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "林可",
        "years_experience": 3.0,
        "skills": list(_RANKED_SKILLS),
        "projects": [{"name": "订单中心", "highlights": ["日峰 2k 单"]}],
    }
    base.update(overrides)
    return base


def _gap(level: str, requirement: str = "精通 Java", **overrides: Any) -> dict[str, Any]:
    gap: dict[str, Any] = {
        "requirement": requirement,
        "level": level,
        "evidence": "Java 精通" if level == "matched" else None,
        "skill": "Java" if "Java" in requirement else None,
        "required_level": "proficient",
        "resume_level": "proficient" if level == "matched" else "familiar",
        "section": "required",
        "source": "lexicon",
    }
    gap.update(overrides)
    return gap


def _dims(
    parsed: dict[str, Any] | None = None, gaps: list[dict[str, Any]] | None = None
) -> list[Dimension]:
    return score_dimensions(parsed or _parsed(), gaps or [])


def _by_name(dimensions: list[Dimension]) -> dict[str, Dimension]:
    return {dimension.name: dimension for dimension in dimensions}


def test_all_five_dimensions_come_back_in_a_fixed_order() -> None:
    dimensions = _dims()

    assert [d.name for d in dimensions] == [
        "skill_breadth",
        "skill_depth",
        "experience",
        "job_fit",
        "project_evidence",
    ]
    assert all(d.label == LABELS[d.name] for d in dimensions)


def test_every_score_is_within_the_rubric_and_carries_an_evidence_sentence() -> None:
    """M4 验收：按 rubric 给每个维度 1–5 分 + 证据句。"""
    for dimension in _dims(gaps=[_gap("missing")]):
        assert 1 <= dimension.score <= 5
        assert dimension.evidence.strip()
        assert dimension.signals


def test_scores_are_monotonic_in_the_obvious_inputs() -> None:
    rich = _parsed(
        skills=[{"name": f"S{i}", "level": "proficient", "evidence": "S 精通"} for i in range(16)],
        years_experience=9.0,
    )
    poor = _parsed(skills=[], years_experience=0.0, projects=[])

    rich_scores = _by_name(_dims(rich))
    poor_scores = _by_name(_dims(poor))
    for name in ("skill_breadth", "skill_depth", "experience", "project_evidence"):
        assert rich_scores[name].score >= poor_scores[name].score
    assert rich_scores["skill_breadth"].score == 5
    assert poor_scores["skill_breadth"].score == 1


def test_skill_depth_reads_degree_words_not_item_count() -> None:
    """一堆"了解"不该比三条"精通"更深——证据句里也要看得懂为什么。"""
    many_shallow = _parsed(
        skills=[{"name": f"S{i}", "level": "basic", "evidence": "了解"} for i in range(10)]
    )
    few_deep = _parsed(skills=list(_RANKED_SKILLS))

    assert _by_name(_dims(few_deep))["skill_depth"].score > _by_name(
        _dims(many_shallow)
    )["skill_depth"].score


def test_job_fit_counts_only_decided_gaps() -> None:
    """`unverified` 不进分母：它既不是命中也不是缺口，算进去会稀释真实命中率。"""
    all_matched = _by_name(_dims(gaps=[_gap("matched"), _gap("matched", "精通 MySQL")]))
    with_unverified = _by_name(
        _dims(gaps=[_gap("matched"), _gap("missing", "精通 MySQL"), _gap("unverified", "沟通能力")])
    )

    assert all_matched["job_fit"].score == 5
    assert with_unverified["job_fit"].signals == {
        "decided": 2,
        "matched": 1,
        "partial": 0,
        "unverified": 1,
    }


def test_no_comparable_requirements_gives_the_middle_grade_not_a_high_one() -> None:
    """没依据时给中档：给 5 分会让 plan 以为岗位方向不用担心。"""
    dimension = _by_name(_dims(gaps=[]))["job_fit"]

    assert dimension.score == 3
    assert "中档" in dimension.evidence


def test_partial_half_counts_between_missing_and_matched() -> None:
    partial = _by_name(_dims(gaps=[_gap("partial")]))["job_fit"]
    missing = _by_name(_dims(gaps=[_gap("missing")]))["job_fit"]

    assert partial.score > missing.score


def test_focus_topics_lead_with_the_weakest_items() -> None:
    gaps = [
        _gap("matched"),
        _gap("missing", "掌握 Redis 缓存设计", skill="Redis"),
        _gap("partial", "熟悉 MySQL 索引", skill="MySQL"),
    ]
    dimensions = _dims(gaps=gaps)

    topics = derive_focus_topics(_parsed(skills=list(_RANKED_SKILLS)), gaps, dimensions)

    assert [topic["score"] for topic in topics] == sorted(topic["score"] for topic in topics)
    assert topics[0]["topic"] == "Redis" or topics[0]["score"] == 1
    assert all(topic["topic"] != "Java" or topic["score"] != 1 for topic in topics)


def test_missing_and_partial_gaps_become_topics_but_matched_ones_do_not() -> None:
    gaps = [
        _gap("matched"),
        _gap("missing", "掌握 Redis", skill="Redis"),
        _gap("partial", "熟悉 Kafka", skill="Kafka"),
    ]

    topics = derive_focus_topics(_parsed(), gaps, _dims(gaps=gaps))

    names = {topic["topic"] for topic in topics}
    assert {"Redis", "Kafka"} <= names
    # Java 会以"自称精通、待验真"的身份出现（source=resume），那是配额有余时的最后一档；
    # 这里要锁的是它别从 JD 那边挤占弱项名额
    assert "Java" not in {topic["topic"] for topic in topics if topic["source"] == "jd"}
    assert all(topic["score"] > 1 for topic in topics if topic["topic"] == "Java")


def test_every_weak_dimension_is_represented_even_with_no_gaps() -> None:
    """弱项"至少各占一槽"的前提：主题清单里得先有它。"""
    parsed = _parsed(skills=[], projects=[], years_experience=0.0)
    dimensions = _dims(parsed)
    weak = [d.name for d in dimensions if d.score <= WEAK_SCORE]

    topics = derive_focus_topics(parsed, [], dimensions)

    assert set(weak) <= {topic["dimension"] for topic in topics}


def test_claimed_expertise_is_queued_last_for_verification() -> None:
    """自称"精通"的技能要排最后：先攻弱项，配额有余才轮到验真。"""
    parsed = _parsed(skills=list(_RANKED_SKILLS))
    gaps = [_gap("missing", "掌握 Redis")]

    topics = derive_focus_topics(parsed, gaps, _dims(parsed, gaps))

    assert topics[0]["source"] == "jd"
    assert topics[-1]["source"] == "resume"
    assert {topic["topic"] for topic in topics if topic["source"] == "resume"} == {
        "Java",
        "Spring Boot",
    }


def test_topic_list_is_capped_and_free_of_duplicates() -> None:
    gaps = [_gap("missing", f"掌握 技能{i}") for i in range(20)]

    topics = derive_focus_topics(_parsed(), gaps, _dims(gaps=gaps))

    assert len(topics) <= MAX_FOCUS_TOPICS
    assert len({topic["topic"] for topic in topics}) == len(topics)


def test_topics_carry_the_provenance_generate_will_need() -> None:
    gaps = [_gap("missing", "掌握 Redis", skill="Redis")]

    (topic,) = [
        topic
        for topic in derive_focus_topics(_parsed(), gaps, _dims(gaps=gaps))
        if topic["topic"] == "Redis"
    ]

    assert set(topic) == {"topic", "dimension", "score", "why", "evidence", "source"}
    assert topic["dimension"] == "job_fit"
    assert topic["why"]


def test_confidence_stays_in_a_reasonable_band_and_grows_with_material() -> None:
    bare = confidence_from_signals({"name": "林可"}, [])
    rich_projects = [{"name": "p", "highlights": ["a", "b", "c"]}]
    full = confidence_from_signals(
        _parsed(skills=list(_RANKED_SKILLS), projects=rich_projects),
        [_gap("matched"), _gap("missing", "掌握 Redis"), _gap("partial", "熟悉 Kafka")],
    )

    assert 0.4 <= bare <= full <= 0.94


def test_confidence_does_not_punish_a_missing_years_field_beyond_its_own_weight() -> None:
    with_years = confidence_from_signals(_parsed(), [])
    without = confidence_from_signals({"name": "林可", "skills": [{"name": "Java"}]}, [])

    assert with_years > without


def test_fallback_narrative_is_a_complete_profile_narrative() -> None:
    """模型没跑成时的兜底叙述：结构必须与模型版一致，字段一个都不能缺。"""
    dimensions = _dims(gaps=[_gap("missing", "掌握 Redis")])
    topics = derive_focus_topics(_parsed(), [_gap("missing", "掌握 Redis")], dimensions)

    narrative = fallback_narrative(_parsed(), dimensions, topics, 0.6)

    assert set(narrative) == {
        "summary",
        "strengths",
        "weaknesses",
        "confidence",
        "narrative_source",
    }
    assert narrative["narrative_source"] == "rubric"
    assert "林可" in narrative["summary"]
    assert 0 <= narrative["confidence"] <= 1


def test_fallback_narrative_survives_an_empty_topic_list() -> None:
    dimensions = _dims(_parsed(skills=[], projects=[]))

    narrative = fallback_narrative(_parsed(skills=[], projects=[]), dimensions, [], 0.4)

    assert narrative["summary"]
    assert isinstance(narrative["strengths"], list)


def test_dirty_parsed_does_not_raise() -> None:
    """画像层收到脏数据时要照常出分——真正该拦的是上游，别让这里 500。"""
    dimensions = _dims({"name": None, "skills": "不是列表", "years_experience": "三年"})

    assert all(1 <= dimension.score <= 5 for dimension in dimensions)
    assert _by_name(dimensions)["skill_breadth"].score == 1, "认不出技能条目就是最低档"
