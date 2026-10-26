"""match 规则层的单测：JD 怎么切、档位怎么判、模型的判定什么时候不算数（全程离线）。

这一层不调模型也不调库，所以断言可以写得非常具体——它就是 `matched_gaps` 的形状契约。
"""

import json
from typing import Any

import pytest

from app.services.match_service import (
    Requirement,
    adopt_llm_gaps,
    build_match,
    make_gap,
    split_requirements,
)

_JD = (
    "岗位职责：负责核心业务的后端服务设计与实现。\n"
    "任职要求：精通 Java，熟悉 Spring Boot；掌握 MySQL 的索引与事务；"
    "了解 Redis 缓存设计；有分布式经验优先。具备较强的沟通与推动能力。\n"
    "加分项：熟悉 Elasticsearch、Kafka 任一即可。"
)
_RAW = "姓名：林可\n技能\nJava 熟悉\nSpring Boot 精通\nMySQL\n良好的跨团队沟通推动能力"


def _parsed(skills: list[dict[str, Any]]) -> dict[str, Any]:
    return {"name": "林可", "years_experience": 3.0, "skills": skills}


def _basic_parsed() -> dict[str, Any]:
    return _parsed(
        [
            {"name": "Java", "level": "familiar", "evidence": "Java 熟悉"},
            {"name": "Spring Boot", "level": "proficient", "evidence": "Spring Boot 精通"},
            {"name": "MySQL", "level": None, "evidence": "MySQL"},
        ]
    )


def test_split_keeps_requirement_text_verbatim_from_the_jd() -> None:
    """每条要求的文本必须能在 JD 原文里找到（去空白后），否则缺口就没法回溯。"""
    from app.extractors.base import squeeze

    jd_squeezed = squeeze(_JD)
    for requirement in split_requirements(_JD):
        assert squeeze(requirement.text) in jd_squeezed


def test_split_routes_lines_into_sections() -> None:
    sections = {item.section for item in split_requirements(_JD)}

    assert sections == {"duty", "required", "preferred"}
    duty = [item for item in split_requirements(_JD) if item.section == "duty"]
    assert len(duty) == 1 and "负责核心业务" in duty[0].text


def test_split_does_not_cut_on_ideographic_comma() -> None:
    """顿号分隔的是同一条要求里的两个技能，切开会让第二个技能丢掉程度词。"""
    line = "任职要求：熟悉 Elasticsearch、Kafka 任一即可。"
    requirements = [item for item in split_requirements(line) if item.section != "required"]
    assert requirements == []
    (only,) = [item for item in split_requirements(line) if item.mentions]
    assert [mention.name for mention in only.mentions] == ["Elasticsearch", "Kafka"]
    assert all(mention.level == "familiar" for mention in only.mentions)


def test_split_strips_numbered_bullets_and_dedupes_repeats() -> None:
    jd = "任职要求：1. 精通 Java；2、精通 Java；（1）精通 Java；- 精通 Java"

    texts = [item.text for item in split_requirements(jd)]

    assert texts == ["精通 Java"]


def test_alignment_grades_by_degree_words() -> None:
    result = build_match(_JD, _basic_parsed())

    assert result.gap_level("Java") == "partial", "JD 要精通、简历只写熟悉"
    assert result.gap_level("Spring Boot") == "matched"
    assert result.gap_level("MySQL") == "partial", "JD 要掌握、简历没写程度词"
    assert result.gap_level("Redis") == "missing"
    assert result.gap_level("分布式") == "missing"


def test_a_requirement_without_degree_word_is_met_by_presence() -> None:
    """"有分布式经验优先"没写程度：简历里提到就算 matched，别再要求一个程度词。"""
    result = build_match("任职要求：有分布式经验优先。", _parsed([
        {"name": "分布式", "level": "basic", "evidence": "了解分布式"}
    ]))

    assert result.gap_level("分布式") == "matched"


def test_every_gap_row_has_the_same_shape() -> None:
    result = build_match(_JD, _basic_parsed())

    for gap in result.gaps:
        assert set(gap) == {
            "requirement",
            "level",
            "evidence",
            "skill",
            "required_level",
            "resume_level",
            "section",
            "source",
        }
        assert gap["level"] in ("matched", "partial", "missing", "unverified")
        assert gap["source"] in ("lexicon", "llm", "unjudged")
        if gap["level"] in ("matched", "partial"):
            assert gap["evidence"], "有证据的档位必须带证据"


def test_duties_are_excluded_and_soft_requirements_go_to_the_model() -> None:
    result = build_match(_JD, _basic_parsed())

    assert all("负责核心业务" not in gap["requirement"] for gap in result.gaps)
    assert [item.text for item in result.unresolved] == ["具备较强的沟通与推动能力"]
    assert any("岗位职责" in note for note in result.notes)


def test_one_skill_produces_one_gap_even_when_required_twice() -> None:
    """同一技能被多处要求时留最严重的那条：`profile` 的命中率不该被重复条目加权。"""
    jd = "任职要求：精通 Java；熟悉 Java。"

    result = build_match(
        jd, _parsed([{"name": "Java", "level": "familiar", "evidence": "Java 熟悉"}])
    )

    assert len(result.gaps) == 1
    assert result.gap_level("Java") == "partial"
    assert result.gaps[0]["requirement"] == "精通 Java"


def test_a_jd_with_no_requirements_is_reported_not_silently_empty() -> None:
    result = build_match("", _basic_parsed())

    assert result.gaps == []
    assert result.notes, "拆不出要求项要留话，否则下游以为这个岗位没要求"


def test_match_is_a_pure_function_of_its_inputs() -> None:
    first = build_match(_JD, _basic_parsed())
    second = build_match(_JD, _basic_parsed())

    assert json.dumps(first.gaps, sort_keys=True) == json.dumps(second.gaps, sort_keys=True)


def test_non_list_skills_do_not_break_the_index() -> None:
    result = build_match(_JD, {"name": "林可", "skills": "不是列表"})

    assert [gap["level"] for gap in result.gaps] == ["missing"] * len(result.gaps)


# --- 模型判定的三道闸 ---

def _unresolved(text: str = "具备较强的沟通与推动能力") -> list[Requirement]:
    return [Requirement(text=text, section="required", mentions=())]


def test_judgement_without_verifiable_evidence_is_downgraded_not_believed() -> None:
    """抽取层铁律：证据必须能在原文逐字找到，否则这个档位当没说过。"""
    gaps, notes = adopt_llm_gaps(
        [{"requirement": "具备较强的沟通与推动能力", "level": "matched", "evidence": "沟通专家"}],
        requirements=_unresolved(),
        raw_text=_RAW,
    )

    assert gaps[0]["level"] == "unverified"
    assert gaps[0]["evidence"] is None
    assert any("找不到" in note for note in notes)


def test_a_real_evidence_quote_is_adopted_and_keeps_whitespace_flexibility() -> None:
    gaps, notes = adopt_llm_gaps(
        [
            {
                "requirement": "具备较强的沟通与推动能力",
                "level": "partial",
                "evidence": "良好的跨团队沟通 推动能力",
            }
        ],
        requirements=_unresolved(),
        raw_text=_RAW,
    )

    assert gaps[0]["level"] == "partial"
    assert gaps[0]["source"] == "llm"
    assert notes == []


def test_requirements_the_model_invented_are_dropped() -> None:
    """"岗位一般都要 Kubernetes"不是我们问它的话——凭空加戏的要求不进清单。"""
    gaps, notes = adopt_llm_gaps(
        [{"requirement": "精通 Kubernetes", "level": "missing", "evidence": None}],
        requirements=_unresolved(),
        raw_text=_RAW,
    )

    assert gaps == []
    assert any("不在待判定清单里" in note for note in notes)


def test_a_paraphrase_that_points_at_exactly_one_requirement_counts() -> None:
    gaps, _ = adopt_llm_gaps(
        [{"requirement": "较强的沟通与推动能力", "level": "missing", "evidence": None}],
        requirements=_unresolved(),
        raw_text=_RAW,
    )

    assert gaps[0]["requirement"] == "具备较强的沟通与推动能力"


def test_an_ambiguous_paraphrase_is_dropped() -> None:
    """两条要求都能接上时，模型其实是在含糊其辞，不该替它猜。"""
    requirements = [
        Requirement(text="具备较强的沟通与推动能力", mentions=()),
        Requirement(text="良好的沟通意识", mentions=()),
    ]

    gaps, notes = adopt_llm_gaps(
        [{"requirement": "沟通", "level": "missing", "evidence": None}],
        requirements=requirements,
        raw_text=_RAW,
    )

    assert gaps == []
    assert notes


def test_a_bogus_level_falls_back_to_unverified() -> None:
    gaps, notes = adopt_llm_gaps(
        [{"requirement": "具备较强的沟通与推动能力", "level": "非常好", "evidence": None}],
        requirements=_unresolved(),
        raw_text=_RAW,
    )

    assert gaps[0]["level"] == "unverified"
    assert any("不在合法档里" in note for note in notes)


def test_missing_needs_no_evidence_and_survives_the_gate() -> None:
    gaps, notes = adopt_llm_gaps(
        [{"requirement": "具备较强的沟通与推动能力", "level": "missing", "evidence": None}],
        requirements=_unresolved(),
        raw_text=_RAW,
    )

    assert gaps[0]["level"] == "missing"
    assert notes == []


def test_make_gap_is_the_single_shape_source() -> None:
    assert set(make_gap("要求", "missing")) == {
        "requirement",
        "level",
        "evidence",
        "skill",
        "required_level",
        "resume_level",
        "section",
        "source",
    }


def test_unresolved_requirements_do_not_split_into_multiple_skills() -> None:
    """"熟悉 Spring Security 与 OAuth2.0" 这类写法里词典能认出两个技能，
    于是它不该出现在待判定清单里。"""
    result = build_match("任职要求：熟悉 Spring Security 与 OAuth 授权。", _parsed([]))

    assert result.unresolved == tuple()
    assert {gap["skill"] for gap in result.gaps} == {"Spring Security", "OAuth"}


@pytest.mark.parametrize("section", ["required", "preferred"])
def test_preferred_requirements_are_marked_so_plan_can_down_weight_them(section: str) -> None:
    label = "任职要求" if section == "required" else "加分项"
    result = build_match(f"{label}：精通 Java。", _parsed([]))

    assert result.gaps[0]["section"] == section
