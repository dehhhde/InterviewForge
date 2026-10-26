"""项目经历抽取的离线测试：切块判据、子标题归桶与证据核对。

不调模型，所以断言全是结构：块数、名字、要点条数、证据能不能回到原文。
"""

from pathlib import Path

import pytest

from app.extractors.base import evidence_belongs
from app.extractors.project_extractor import extract_projects
from app.parsers.pdf_parser import PdfParser

REAL_RESUME = Path(__file__).parent.parent / "fixtures" / "resumes" / "附件简历-梁清生.pdf"

_TWO_PROJECTS = "\n".join(
    [
        "项目经历",
        "库存中心 2023.01-2023.06",
        "项目描述",
        "为三个仓库提供统一的出入库接口。",
        "技术栈",
        "Spring Boot、Redis",
        "核心职责与成果",
        "1. 把扣减接口 P99 从 180ms 降到 45ms",
        "2. 引入令牌桶限流",
        "对象存储中心 2025.03-2025.07",
        "项目描述",
        "面向内部系统的文件服务平台。",
        "项目成果",
        "1、支持多租户多桶管理",
    ]
)


def test_a_dated_line_opens_a_block_and_its_date_stays_out_of_the_name() -> None:
    result = extract_projects(_TWO_PROJECTS)

    assert [p.name for p in result.projects] == ["库存中心", "对象存储中心"]
    assert result.notes == ()


def test_sub_headers_route_lines_into_summary_and_highlights() -> None:
    project = extract_projects(_TWO_PROJECTS).projects[0]

    assert project.summary is not None
    assert project.summary.startswith("为三个仓库")
    assert "技术栈：Spring Boot、Redis" in project.summary
    assert project.highlights == [
        "1. 把扣减接口 P99 从 180ms 降到 45ms",
        "2. 引入令牌桶限流",
    ]


def test_unnumbered_outcome_lines_end_up_as_highlights_too() -> None:
    project = extract_projects(_TWO_PROJECTS).projects[1]

    assert project.highlights == ["1、支持多租户多桶管理"]
    assert project.summary == "面向内部系统的文件服务平台。"


def test_evidence_is_the_whole_block_and_must_be_findable_in_the_original() -> None:
    result = extract_projects(_TWO_PROJECTS)

    for project in result.projects:
        assert evidence_belongs(_TWO_PROJECTS, project.evidence)
    assert "180ms" in result.projects[0].evidence


def test_body_before_the_first_title_is_dropped_with_a_note_not_invented_a_name() -> None:
    raw = "项目经历\n这是一段没有项目名开头的说明。\n库存中心 2023.01-2023.06\n项目描述\n做出入库"

    result = extract_projects(raw)

    assert [p.name for p in result.projects] == ["库存中心"]
    assert any("找不到项目名" in note for note in result.notes)


def test_a_section_without_any_date_falls_back_to_layout_and_says_so() -> None:
    raw = "项目经历\n智能网关\n项目描述\n统一鉴权与限流。"

    result = extract_projects(raw)

    assert [p.name for p in result.projects] == ["智能网关"]
    assert any("按版式猜" in note for note in result.notes)


def test_a_wrapped_prose_line_is_not_mistaken_for_a_project_name() -> None:
    """换行剩下的正文尾巴后面紧跟"技术栈"，但它带句读，所以不是标题。"""
    raw = "\n".join(
        [
            "项目经历",
            "库存中心 2023.01-2023.06",
            "为三个仓库提供统一的出入库接口，并",
            "加水印等功能的 SDK 接入能力。",
            "技术栈",
            "Spring Boot",
        ]
    )

    result = extract_projects(raw)

    assert [p.name for p in result.projects] == ["库存中心"]
    assert "加水印等功能的 SDK 接入能力。" in (result.projects[0].summary or "")


def test_missing_section_is_reported_instead_of_guessing_from_the_whole_text() -> None:
    result = extract_projects("姓名：陈昊\n13800138000 chen@example.com\n熟悉 Spring Boot")

    assert result.projects == []
    assert any("没找到项目经历栏目" in note for note in result.notes)


def test_the_real_resume_splits_into_two_projects_with_evidence() -> None:
    if not REAL_RESUME.is_file():
        pytest.skip("缺少真实简历样本")
    raw = PdfParser().parse(REAL_RESUME).raw_text

    result = extract_projects(raw)

    assert [p.name for p in result.projects] == ["OA办公自动化系统", "对象存储中心"]
    assert [len(p.highlights) for p in result.projects] == [17, 6]
    assert all(evidence_belongs(raw, p.evidence) for p in result.projects)
    assert all(p.summary for p in result.projects)
