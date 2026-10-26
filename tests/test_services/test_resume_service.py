"""校验规则层的单测：四条规则各自报错、机械修复各自生效（全程离线，不调模型）。

断言的是"哪条规则在什么输入下开口"，不锁死文案细节——消息里只核对关键字。
"""

from datetime import date
from typing import Any

import pytest

from app.extractors.timeline_extractor import extract_timeline
from app.schemas.resume import ResumeSchema
from app.services.resume_service import ValidationOutcome, validate_resume

_TODAY = date(2024, 1, 1)
_RESUME = """姓名：陈昊 13800138000 chen@example.com

技能
Java 精通 Spring Boot 熟练

工作经历
甲公司 后端开发 2021.07-2023.06
    负责订单服务
乙公司 高级开发 2023.07-至今

教育经历
某大学 本科 软件工程 2017-2021
"""


def _computed_years() -> float:
    """干净用例的年限就该等于时间线复算值，否则测试在替规则编数据。"""
    timeline = extract_timeline(_RESUME, today=_TODAY)
    assert timeline.years_experience is not None
    return round(float(timeline.years_experience.value), 1)


def _clean_parsed() -> dict[str, Any]:
    return ResumeSchema.model_validate(
        {
            "name": "陈昊",
            "phone": "13800138000",
            "email": "chen@example.com",
            "years_experience": _computed_years(),
            "skills": [{"name": "Java", "level": "proficient", "evidence": "Java"}],
            "educations": [
                {
                    "school": "某大学",
                    "degree": "bachelor",
                    "major": "软件工程",
                    "start_year": 2017,
                    "graduate_year": 2021,
                    "evidence": "某大学 本科 软件工程 2017-2021",
                }
            ],
            "work_experiences": [
                {
                    "company": "甲公司",
                    "role": "后端开发",
                    "start": "2021-07-01",
                    "end": "2023-06-30",
                    "evidence": "甲公司 后端开发 2021.07-2023.06",
                },
                {
                    "company": "乙公司",
                    "role": "高级开发",
                    "start": "2023-07-01",
                    "evidence": "乙公司 高级开发 2023.07-至今",
                },
            ],
            "projects": [],
        }
    ).model_dump(mode="json")


def _validate(
    raw_text: str = _RESUME, parsed: dict[str, Any] | None = None
) -> ValidationOutcome:
    return validate_resume(raw_text, _clean_parsed() if parsed is None else parsed, today=_TODAY)


def test_a_clean_resume_raises_nothing_and_changes_nothing() -> None:
    parsed = _clean_parsed()

    outcome = _validate(parsed=parsed)

    assert outcome.issues == ()
    assert outcome.repairs == ()
    assert outcome.parsed == parsed


def test_the_input_dict_is_never_mutated() -> None:
    """节点契约：修正在副本上做，原 `parsed` 要留着对比与回放。"""
    parsed = _clean_parsed()
    before = parsed["skills"]

    _validate(parsed=parsed)

    assert parsed["skills"] is before
    assert len(parsed["skills"]) == 1


def test_an_inverted_work_range_is_reported_even_though_it_never_reached_parsed() -> None:
    """倒挂条目被 `WorkExperience._check_date_order` 挡在 schema 之外，
    规则只能从抽取器的丢弃记录里看到它。"""
    dirty = _RESUME.replace("甲公司 后端开发 2021.07-2023.06", "甲公司 后端开发 2023.05-2022.01")

    outcome = _validate(dirty)

    assert any("倒挂" in issue and "2023.05-2022.01" in issue for issue in outcome.issues)


def test_an_entry_whose_evidence_is_not_in_the_original_text_is_deleted_as_a_repair() -> None:
    parsed = _clean_parsed()
    parsed["projects"] = [{"name": "刷单系统", "evidence": "负责刷单系统的全部架构"}]

    outcome = _validate(parsed=parsed)

    assert outcome.parsed["projects"] == []
    assert outcome.issues == (), "幻觉条目就地删掉，不值得为此重跑 parse"
    assert len(outcome.repairs) == 1
    assert "projects" in outcome.repairs[0]


def test_a_padded_skill_level_survives_because_the_rule_layer_does_not_judge_wording() -> None:
    parsed = _clean_parsed()
    parsed["skills"].append(
        {"name": "Kafka", "level": "proficient", "evidence": "乙公司 高级开发 2023.07-至今"}
    )

    assert _validate(parsed=parsed).issues == ()


def test_years_padded_to_zero_are_backfilled_from_the_timeline() -> None:
    parsed = _clean_parsed()
    parsed["years_experience"] = 0.0

    outcome = _validate(parsed=parsed)

    assert outcome.parsed["years_experience"] == _computed_years()
    assert any("补成" in note for note in outcome.repairs)
    assert outcome.issues == ()


def test_years_that_disagree_with_the_timeline_are_reported_not_silently_fixed() -> None:
    parsed = _clean_parsed()
    parsed["years_experience"] = _computed_years() + 5.0

    outcome = _validate(parsed=parsed)

    assert any("自洽" in issue or "差" in issue for issue in outcome.issues)
    assert outcome.parsed["years_experience"] == _computed_years() + 5.0, "差太多就交给人判，别擅改"


def test_missing_required_fields_are_reported() -> None:
    parsed = _clean_parsed()
    parsed["name"] = "  "
    del parsed["years_experience"]

    outcome = _validate(parsed=parsed)

    assert any("姓名" in issue for issue in outcome.issues)
    assert any("工作年限" in issue for issue in outcome.issues)


def test_a_degree_outside_the_enum_is_reported() -> None:
    parsed = _clean_parsed()
    parsed["educations"][0]["degree"] = "doctor"

    outcome = _validate(parsed=parsed)

    assert any("枚举" in issue for issue in outcome.issues)


def test_a_degree_word_the_alias_table_does_not_cover_is_reported_as_a_gap() -> None:
    """`MBA` 在抽取词表里但不在别名表里，会被静默归成 other——枚举合法但信息丢了。"""
    dirty = _RESUME.replace("某大学 本科 软件工程 2017-2021", "某大学 MBA 2017-2021")
    parsed = _clean_parsed()
    parsed["educations"][0].update(
        {"degree": "other", "evidence": "某大学 MBA 2017-2021", "major": None}
    )

    outcome = _validate(dirty, parsed)

    assert any("MBA" in issue and "别名表" in issue for issue in outcome.issues)


def test_without_any_extraction_there_is_nothing_to_validate() -> None:
    outcome = validate_resume(_RESUME, None)

    assert outcome.parsed == {}
    assert len(outcome.issues) == 1
    assert "抽取结果" in outcome.issues[0]


@pytest.mark.parametrize(
    "key",
    ["skills", "educations", "work_experiences", "projects"],
)
def test_every_evidenced_list_gets_the_same_evidence_gate(key: str) -> None:
    parsed = _clean_parsed()
    item = dict(parsed[key][0]) if parsed[key] else {"evidence": "查无此段"}
    item["evidence"] = "原文里绝对不存在的一句话"
    parsed[key] = [item]

    outcome = _validate(parsed=parsed)

    assert outcome.parsed[key] == []
    assert any(key in note for note in outcome.repairs)
