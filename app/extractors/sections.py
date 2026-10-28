"""简历栏目切分：把 `raw_text` 拆成 education / work / project / skills 四段〔§8.6〕

中文简历的栏目名极其随意（"教育经历"/"教育背景"/"学习经历"…），所以这里只维护一张
别名词表：认出来的归对应段，认不出但**长得像标题**的行留给调用方写进 `notes`——
宁可提示"该补词典了"，也不要悄悄把项目经历当成工作经历。

另外 PDF 会把一行拆成两行（排版换行不是语义换行）。这里**不做行合并**：判据怎么定都会
误伤"日期行后面跟正文"的情形，而抽取器本来就是按行取证据的，拆开不影响命中。
待办记在 `docs/TODO.md` §1。
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from app.extractors.base import collapse

SectionName = Literal["education", "work", "project", "skills"]
SubName = Literal["summary", "stack", "outcome"]

#: 段名 -> 可能的标题写法（短行包含任一词即算）
SECTION_KEYWORDS: dict[SectionName, tuple[str, ...]] = {
    "education": ("教育经历", "教育背景", "学习经历", "学历背景", "教育情况", "教育"),
    "work": ("工作经历", "工作经验", "职业经历", "从业经历", "实习经历", "工作履历"),
    "project": ("项目经历", "项目经验", "项目作品", "项目展示", "主要项目"),
    "skills": ("专业技能", "技能特长", "技能清单", "个人技能", "技术能力", "专业能力", "技能"),
}
#: 段内的子标题，project 抽取器用它分"描述 / 技术栈 / 成果"
SUBSECTION_KEYWORDS: dict[SubName, tuple[str, ...]] = {
    "summary": ("项目描述", "项目简介", "项目背景", "描述"),
    "stack": ("技术栈", "技术架构", "关键技术", "技术选型"),
    "outcome": ("核心职责", "项目成果", "主要贡献", "职责", "成果"),
}
#: 长得像标题却没被词典命中，用于"该补词典了"的提示
HEADER_HINTS: tuple[str, ...] = (
    "经历", "经验", "背景", "技能", "能力", "特长", "简介", "评价",
    "总结", "证书", "荣誉", "获奖", "语言", "个人情", "活动",
)
#: 标题行不会带句读；"调优实践经验。" 是换行剩下的正文尾巴，不是栏目名
_SENTENCE_PUNCTUATION = "。；，,！!？?"
_HEADER_MAX_CHARS = 12
_BULLET_RE = re.compile(r"^\s*(?:\d{1,2}\s*[.、)）]|[-•·*▪‣])")


@dataclass(frozen=True)
class SectionMap:
    """按栏目归好类的行。段内保留子标题原文，供项目抽取器当标记用。"""

    sections: dict[SectionName, list[str]] = field(default_factory=dict)
    #: 第一个栏目标题之前的行（姓名与联系方式通常在头图区）
    preamble: list[str] = field(default_factory=list)
    #: 像标题却没被认出的短行——调用方应把它写进 notes
    unknown_headers: list[str] = field(default_factory=list)

    def lines_of(self, name: SectionName) -> list[str]:
        """段内的行。**返回副本**：这份 `SectionMap` 现在要跨抽取器共用（T-208），
        交出内部列表等于给任何一个抽取器"改坏别人输入"的机会。"""
        return list(self.sections.get(name, []))


def is_bullet(line: str) -> bool:
    return bool(_BULLET_RE.match(line))


def header_kind(line: str) -> tuple[SectionName | None, SubName | None]:
    """（栏目标题, 子标题），至多一个非 None。三条守卫：短、无数字、命中词表。

    "2年工作经验 | 求职意向：Java" 既含"工作经验"又是正文行，靠长度与数字两道拦下；
    少了这两道，头图那两行会把整份简历误判成工作经历段。
    """
    stripped = collapse(line)
    if not stripped or len(stripped) > _HEADER_MAX_CHARS:
        return None, None
    if any(ch.isdigit() for ch in stripped):
        return None, None
    for section, keywords in SECTION_KEYWORDS.items():
        if any(keyword in stripped for keyword in keywords):
            return section, None
    for sub, keywords in SUBSECTION_KEYWORDS.items():
        if any(keyword in stripped for keyword in keywords):
            return None, sub
    return None, None


def looks_like_header(line: str) -> bool:
    """像标题但词典没命中——只用于"该补词典了"的提示，判据比 `header_kind` 松。

    以句读收尾的一定是正文碎片（"调优实践经验。"），别拿它吓唬用户。
    """
    stripped = collapse(line)
    if not stripped or stripped[-1] in _SENTENCE_PUNCTUATION:
        return False
    if len(stripped) > _HEADER_MAX_CHARS or any(ch.isdigit() for ch in stripped):
        return False
    return any(hint in stripped for hint in HEADER_HINTS)


def merge_wrapped(lines: Iterable[str]) -> list[str]:
    """丢掉空行，保留原文一行。（PDF 会把一行拆成两行，见 docs/TODO.md §1 的行合并待办）"""
    return [line.strip() for line in lines if line.strip()]


def split_sections(raw_text: str) -> SectionMap:
    result = SectionMap()
    current: SectionName | None = None
    for line in merge_wrapped(raw_text.splitlines()):
        section, sub = header_kind(line)
        if section is not None:
            current = section
            continue
        if sub is None and looks_like_header(line):
            result.unknown_headers.append(line)
        if current is None:
            result.preamble.append(line)
        else:
            result.sections.setdefault(current, []).append(line)
    return result
