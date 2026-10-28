"""项目经历抽取的离线测试：切块判据、子标题归桶与证据核对。

不调模型，所以断言全是结构：块数、名字、要点条数、证据能不能回到原文。
"""

from pathlib import Path

import pytest

from app.extractors.base import evidence_belongs, squeeze
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
    # 12 = 17 行编号行 - 5 行分组标题（T-103）。第二个项目 6 行全是条目，一条没掉。
    assert [len(p.highlights) for p in result.projects] == [12, 6]
    assert all(evidence_belongs(raw, p.evidence) for p in result.projects)
    assert all(p.summary for p in result.projects)


def test_a_numbered_sub_header_opens_a_group_instead_of_becoming_a_highlight() -> None:
    """`1. 权限管理体系` 是分组标题，`1、结合 RBAC 模型控制粒度` 才是成果条目。

    断言只看归属：标题行不出现在 `highlights`，它后面的正文归进该桶。
    """
    raw = "\n".join(
        [
            "项目经历",
            "OA办公自动化系统 2025.07-2026.07",
            "核心职责与成果",
            "1. 权限管理体系",
            "结合 RBAC + ABAC 权限模型，支持多维度细粒度控制",
            "2、设计自适应直传策略",
            "对象存储中心 2025.03-2025.07",
            "项目成果",
            "1、支持多租户多桶管理",
        ]
    )

    projects = extract_projects(raw).projects

    assert [p.highlights for p in projects] == [
        ["结合 RBAC + ABAC 权限模型，支持多维度细粒度控制", "2、设计自适应直传策略"],
        ["1、支持多租户多桶管理"],
    ]
    assert projects[0].evidence.count("权限管理体系") == 1, "标题仍是证据的一部分，只是不当要点"


def test_a_numbered_item_with_punctuation_stays_a_highlight() -> None:
    """分组标题判据的反面：带句读或动作词开头的编号行是成果条目，一条都不能丢。"""
    raw = "\n".join(
        [
            "项目经历",
            "库存中心 2023.01-2023.06",
            "核心职责与成果",
            "1、设计自适应直传、分片上传策略选择器，并解决上下文传递",
            "2、引入令牌桶限流",
        ]
    )

    result = extract_projects(raw)

    assert result.projects[0].highlights == [
        "1、设计自适应直传、分片上传策略选择器，并解决上下文传递",
        "2、引入令牌桶限流",
    ]
    assert result.notes == (), "正常的编号条目不该产出流水账"


def test_the_real_resume_project_description_invents_no_punctuation() -> None:
    """T-102 的验收（项目侧）：描述桶内的连接只补空格。

    改之前这里出现过"目前服务于；本公司及母公司"与"Casdoor(用户中心)、；Springsecurity"。
    比对像只取描述那一段——"技术栈："是这条链有意加的话题标签，本来就不在原文里。
    """
    if not REAL_RESUME.is_file():
        pytest.skip("缺少真实简历样本")
    raw = PdfParser().parse(REAL_RESUME).raw_text
    haystack = squeeze(raw)

    descriptions = [
        (project.summary or "").split("；")[0] for project in extract_projects(raw).projects
    ]

    assert len(descriptions) == 2
    for description in descriptions:
        assert squeeze(description) in haystack
