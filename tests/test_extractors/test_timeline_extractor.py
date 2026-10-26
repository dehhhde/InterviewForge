"""时间线抽取的离线测试：区间语法、段落归属与年限复算。

真简历那份是 `附件简历-梁清生.pdf`，只用它的文字层（不联网、不调模型）。
"""

from datetime import date
from pathlib import Path

import pytest

from app.extractors.timeline_extractor import extract_timeline, parse_range
from app.parsers.pdf_parser import PdfParser

REAL_RESUME = Path(__file__).parent.parent / "fixtures" / "resumes" / "附件简历-梁清生.pdf"


def _resume_text() -> str:
    if not REAL_RESUME.is_file():
        pytest.skip("缺少真实简历样本")
    return PdfParser().parse(REAL_RESUME).raw_text


@pytest.mark.parametrize(
    ("line", "start", "end"),
    [
        ("广东裕太科技有限公司 后端开发 2024.06-2026.06", date(2024, 6, 1), date(2026, 6, 30)),
        ("某公司 开发 2021-2025", date(2021, 1, 1), date(2025, 12, 31)),
        ("某公司 开发 2024/6~2025/7", date(2024, 6, 1), date(2025, 7, 31)),
        ("某公司 开发 2024年6月-2025年1月", date(2024, 6, 1), date(2025, 1, 31)),
        ("某公司 开发 2021.09 至今", date(2021, 9, 1), None),
        ("某公司 开发 2021年-现在", date(2021, 1, 1), None),
    ],
)
def test_the_date_shapes_resumes_actually_use(line: str, start: date, end: date | None) -> None:
    found = parse_range(line)

    assert found is not None
    assert (found.start, found.end) == (start, end)
    assert found.head == "某公司 开发" or found.head == "广东裕太科技有限公司 后端开发"


@pytest.mark.parametrize(
    "line",
    ["2024 年双十一大促期间负责稳定性", "订单号 20241231001", "支撑 360 评估、10+ 模块"],
)
def test_numbers_that_are_not_ranges_do_not_become_work_periods(line: str) -> None:
    assert parse_range(line) is None


def test_only_dated_lines_open_a_work_entry_and_the_rest_become_summary() -> None:
    raw = "\n".join(
        [
            "工作经历",
            "甲公司 后端开发 2020.01-2022.03",
            "负责订单域接口",
            "带过 3 个人",
            "乙公司 架构师 2022.04-至今",
            "主导网关重构",
        ]
    )

    result = extract_timeline(raw, today=date(2024, 1, 1))

    assert [w.company for w in result.work_experiences] == ["甲公司", "乙公司"]
    assert [w.role for w in result.work_experiences] == ["后端开发", "架构师"]
    assert result.work_experiences[0].summary == "负责订单域接口；带过 3 个人"
    assert result.work_experiences[1].end is None, "在职不等于没抽到日期"
    assert result.years_experience is not None
    assert float(result.years_experience.value) == pytest.approx(4.0, abs=0.1)


def test_a_reversed_range_is_dropped_with_a_note_instead_of_being_silently_fixed() -> None:
    raw = "工作经历\n甲公司 后端开发 2023.05-2022.01\n负责订单域接口"

    result = extract_timeline(raw)

    assert result.work_experiences == []
    assert result.years_experience is None
    assert any("倒挂" in note for note in result.notes)


def test_overlapping_stints_are_merged_before_years_are_summed() -> None:
    """实习与全职叠在一年里，不该把这一年算两遍。"""
    raw = "\n".join(
        [
            "工作经历",
            "A 公司 实习 2023.01-2023.12",
            "B 公司 开发 2023.06-2024.06",
        ]
    )

    result = extract_timeline(raw, today=date(2025, 1, 1))

    assert result.years_experience is not None
    assert float(result.years_experience.value) == pytest.approx(1.5, abs=0.1)
    assert any("重叠" in note for note in result.notes)
    assert result.years_experience.evidence.count("+") == 1, "证据要写清是这两段算出来的"


def test_education_lines_share_the_work_grammar_and_split_into_school_degree_major() -> None:
    raw = "教育经历\n广州应用科技学院 本科 软件工程 2021-2025"

    result = extract_timeline(raw)

    education = result.educations[0]
    assert (education.school, education.degree, education.major) == (
        "广州应用科技学院",
        "bachelor",
        "软件工程",
    )
    assert (education.start_year, education.graduate_year) == (2021, 2025)


def test_an_education_line_without_a_degree_word_falls_back_to_other() -> None:
    result = extract_timeline("教育经历\n某大学 计算机 2018-2022")

    assert result.educations[0].degree == "other"
    assert result.educations[0].major == "计算机"


def test_resume_fields_are_dumped_ready_for_the_schema() -> None:
    result = extract_timeline("工作经历\n甲公司 后端开发 2020.01-2022.03")

    fields = result.as_resume_fields()

    assert fields["work_experiences"][0]["start"] == "2020-01-01"
    assert "educations" in fields


def test_the_real_resume_yields_one_job_one_degree_and_a_computed_year_count() -> None:
    result = extract_timeline(_resume_text(), today=date(2026, 10, 16))

    assert [(w.company, w.role) for w in result.work_experiences] == [
        ("广东裕太科技有限公司", "后端开发工程师（偏全栈）")
    ]
    assert result.work_experiences[0].start == date(2024, 6, 1)
    assert [e.school for e in result.educations] == ["广州应用科技学院"]
    assert result.years_experience is not None
    assert float(result.years_experience.value) == pytest.approx(2.1, abs=0.1)
    assert result.notes == ()
