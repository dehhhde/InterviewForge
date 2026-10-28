"""`base.join_wrapped_lines` 的离线测试：换行连接只补空格，不补语义标点〔T-102〕

判据放在抽取层公共件里，是因为工作摘要与项目描述是同一条 PDF 换行问题的两个受害者；
两侧各写一遍就会有两套标准（与 AGENTS.md §5 的引用判据同一条理由）。
"""

from collections.abc import Sequence

import pytest

from app.extractors.base import join_wrapped_lines, squeeze


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        # 行末没有标点：只补空格，绝不补"；"
        (["成功验收交给市场部推", "广"], "成功验收交给市场部推 广"),
        # 行末已有分隔标点：直拼，一个字符都不多补
        (["成功推动web端、", "app端系统上线"], "成功推动web端、app端系统上线"),
        (["面向内部系统，", "提供文件服务"], "面向内部系统，提供文件服务"),
        (["负责导出功能。", "支持多格式"], "负责导出功能。支持多格式"),
        # 半角标点同样算"这一行自己收口了"
        (["Spring Boot,", "Redis"], "Spring Boot,Redis"),
        # 空行不参与拼接（PDF 的分页会留下空串）
        (["甲", "", "  ", "乙"], "甲 乙"),
    ],
)
def test_the_joiner_only_adds_a_space_when_the_line_ends_mid_sentence(
    lines: Sequence[str], expected: str
) -> None:
    assert join_wrapped_lines(lines) == expected


def test_an_empty_block_joins_to_an_empty_string() -> None:
    assert join_wrapped_lines([]) == ""
    assert join_wrapped_lines(["", "   "]) == ""


def test_the_joiner_invents_no_characters() -> None:
    """抹掉空白后，拼接结果必须与"把这些行直接串起来"完全一致。

    这是这条判据的底线：连接符只影响可读性，一旦它引入了原文没有的字，
    `evidence_belongs` 那一类核对就会跟着失真。
    """
    lines = ["第一段结尾没有标点", "第二段以、收尾、", "第三段"]

    assert squeeze(join_wrapped_lines(lines)) == squeeze("".join(lines))
