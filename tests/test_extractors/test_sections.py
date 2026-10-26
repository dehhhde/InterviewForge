"""`sections.py` 的离线测试：栏目切分只断言结构，不涉及任何模型输出。"""

from app.extractors.sections import (
    collapse,
    header_kind,
    is_bullet,
    looks_like_header,
    split_sections,
)

_FIVE_LINES = """梁清生
男 | 年龄：23岁 | 17626460250
2年工作经验 | 求职意向：Java
教育经历
广州应用科技学院 本科 软件工程 2021-2025
工作经历
广东裕太科技有限公司 后端开发 2024.06-2026.06
负责 OA 系统
专业技能
熟悉 Spring Boot
"""


def test_headers_are_only_recognised_when_the_line_is_short_and_digit_free() -> None:
    """头图那两行含"工作经验"，但带数字又长，绝不能被当成栏目名。"""
    assert header_kind("工作经历") == ("work", None)
    assert header_kind("项目描述") == (None, "summary")
    assert header_kind("2年工作经验 | 求职意向：Java") == (None, None)
    assert header_kind("负责 OA 系统 2024 年的事") == (None, None)


def test_wrapped_prose_fragment_is_not_reported_as_a_missed_header() -> None:
    """"调优实践经验。" 是换行剩下的尾巴，别把它当成"该补词典"的栏目名吓唬人。"""
    assert looks_like_header("调优实践经验。") is False
    assert looks_like_header("校园活动") is True
    assert looks_like_header("负责过订单、库存等十个模块的接口设计工作") is False


def test_lines_land_in_the_section_they_follow() -> None:
    """子标题留在段内当标记，大栏目切换段。"""
    sections = split_sections(_FIVE_LINES)

    assert sections.preamble[:3] == [
        "梁清生",
        "男 | 年龄：23岁 | 17626460250",
        "2年工作经验 | 求职意向：Java",
    ]
    assert [e[:6] for e in sections.lines_of("education")] == ["广州应用科技"]
    assert sections.lines_of("work") == [
        "广东裕太科技有限公司 后端开发 2024.06-2026.06",
        "负责 OA 系统",
    ]
    assert sections.lines_of("skills") == ["熟悉 Spring Boot"]
    assert sections.lines_of("project") == []


def test_collapse_only_touches_whitespace() -> None:
    assert collapse("  负责   OA\u200b  系统 \n") == "负责 OA 系统"


def test_bullet_detector_covers_the_numbered_style_resumes_use() -> None:
    assert is_bullet("1. 权限管理体系")
    assert is_bullet("2、实现双通道")
    assert is_bullet("- 搭建网关")
    assert not is_bullet("搭建统一处理审计的基座")


def test_blank_lines_are_dropped_before_anything_else() -> None:
    assert split_sections("\n\n  \n工作经历\n某公司 开发 2020-2024\n").lines_of("work") == [
        "某公司 开发 2020-2024"
    ]
