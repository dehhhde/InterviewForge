"""技能抽取的离线测试：词典命中、就近程度词、去重与"该补词典"提示。

技能层不调模型，所以这里的断言全是结构与计数，没有任何文案依赖。
"""

from pathlib import Path

import pytest

from app.extractors.skill_extractor import LEVEL_WORDS, SKILL_ALIASES, extract_skills
from app.parsers.pdf_parser import PdfParser

REAL_RESUME = Path(__file__).parent.parent / "fixtures" / "resumes" / "附件简历-梁清生.pdf"

_SKILLS = "\n".join(
    [
        "专业技能",
        "熟悉 MySQL、PostgreSQL 索引原理，掌握 Redis 持久化机制",
        "精通 JVM 调优，了解 Docker",
        "Java 基础扎实，使用 Spring Boot 搭建服务",
        "熟悉 MySQL 主从复制",
    ]
)


def _names(result) -> set[str]:
    return {item.name for item in result.skills}


def _level(result, name: str) -> str | None:
    return next(item.level for item in result.skills if item.name == name)


def test_the_level_is_the_nearest_degree_word_to_the_left_of_a_name() -> None:
    result = extract_skills(_SKILLS)

    assert _level(result, "MySQL") == "familiar"
    assert _level(result, "Redis") == "familiar", "换到「掌握」之前就该按掌握算"
    assert _level(result, "JVM") == "proficient"
    assert _level(result, "Docker") == "basic"
    assert _level(result, "Java") is None, "左边没有程度词就别猜档位"


def test_a_longer_alias_wins_and_the_same_skill_is_never_emitted_twice() -> None:
    """`Spring Boot 搭建服务` 里的 Spring 不该再单独算一门技能。"""
    result = extract_skills(_SKILLS)

    assert "Spring Boot" in _names(result)
    assert "Spring" not in _names(result)
    assert len([item for item in result.skills if item.name == "MySQL"]) == 1


def test_evidence_is_the_line_the_alias_was_found_on() -> None:
    result = extract_skills(_SKILLS)

    redis = next(item for item in result.skills if item.name == "Redis")
    assert "掌握 Redis" in redis.evidence


def test_the_whole_text_is_scanned_when_there_is_no_skills_section() -> None:
    result = extract_skills("求职意向：Java 后端\n13800138000 chen@example.com")

    assert _names(result) >= {"Java"}
    assert any("技能栏目" in note for note in result.notes)


def test_a_longer_degree_word_beats_its_own_suffix_when_they_end_together() -> None:
    """`熟练运用` 里的 `运用` 与整词在同处以右端收口：并列时比长度〔T-006〕

    旧判据只比起点，起点更晚的 `运用`(basic) 就赢了整词 `熟练运用`(familiar)，
    档位被读低一级；这处判据同时供 match 档位与 rubric 的 `skill_depth` 用。
    """
    result = extract_skills("专业技能\n熟练运用 MySQL")

    assert _level(result, "MySQL") == "familiar"


def test_a_closer_degree_word_still_wins_when_it_is_shorter() -> None:
    """长度只在右端并列时起作用：不等就仍然就近——离技能名越近越像在说它。"""
    result = extract_skills("专业技能\n熟练运用 Redis，并且了解 MySQL")

    assert _level(result, "Redis") == "familiar"
    assert _level(result, "MySQL") == "basic"


def test_each_skill_takes_the_degree_word_right_before_it() -> None:
    """同一行两个档位词：各取各的，不做整行统一档位。"""
    result = extract_skills("专业技能\n了解 Redis，精通 MySQL")

    assert _level(result, "Redis") == "basic"
    assert _level(result, "MySQL") == "proficient"


def test_unknown_looking_product_names_are_reported_but_never_invented_as_skills() -> None:
    result = extract_skills("专业技能\n熟悉 Flowable 与 TransmittableThreadLocal 源码")

    assert "Flowable" in _names(result)
    assert "TransmittableThreadLocal" not in _names(result)
    assert "TransmittableThreadLocal" in result.unmapped


def test_lowercase_fragments_are_not_treated_as_product_names() -> None:
    result = extract_skills("专业技能\n熟悉 the web app 的构建流程，掌握 Nacos")

    assert "Nacos" in _names(result)
    assert result.unmapped == ()


def test_go_needs_its_capital_and_a_bare_word_inside_another_word_does_not_count() -> None:
    """小写的 go 是英文碎词，"logo" 里的 go 也不是编程语言。"""
    result = extract_skills("专业技能\n熟悉 Go 语言，做过 logo 设计")

    assert _names(result) == {"Go"}


def test_no_hit_at_all_says_so_instead_of_returning_a_quiet_empty_list() -> None:
    result = extract_skills("专业技能\n擅长沟通，抗压能力强")

    assert result.skills == []
    assert any("都没命中" in note for note in result.notes)


def test_the_dictionaries_are_shaped_as_the_schema_expects() -> None:
    """别名表与程度表都是"写错就直接进 schema 报错"的东西，所以在这儿钉死形状。"""
    assert all(key == key.lower() and key.strip() for key in SKILL_ALIASES)
    assert all(name.strip() for name in SKILL_ALIASES.values())
    assert set(LEVEL_WORDS.values()) <= {"basic", "familiar", "proficient"}


def test_the_real_resume_exposes_the_stack_the_candidate_wrote_down() -> None:
    if not REAL_RESUME.is_file():
        pytest.skip("缺少真实简历样本")

    result = extract_skills(PdfParser().parse(REAL_RESUME).raw_text)

    assert _names(result) >= {"Java", "Spring Boot", "MyBatis-Plus", "Redis", "PostgreSQL"}
    assert all(len(item.evidence) >= 2 for item in result.skills)
    assert result.notes == ()
