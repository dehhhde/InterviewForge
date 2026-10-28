"""抽取层公共件：`Evidence` 值型 + 证据核对〔阶段 5 · §8.6〕

四个抽取器都受同一条铁律约束：**值必须能被原文佐证**。正则命中的片段天然就是证据；
LLM 给的值必须把它抄的片段一并交回来，核对不上的当幻觉丢弃——这是 `schemas/resume.py`
里每个条目都带 `evidence` 字段的原因。

这里只放真正跨模块的几样东西，不做 `Extractor` 基类：四个抽取器的返回形状差别太大
（联系人是标量、技能是列表、年限是算出来的），强行统一接口只会更难读。
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

Source = Literal["regex", "llm"]

_WHITESPACE_RE = re.compile(r"\s+")
#: 零宽字符（U+200B 系列、BOM、word joiner）：网页复制的简历与 Word 都会塞，
#: 留着会让证据核对莫名其妙地失败，所以一律先删
_ZERO_WIDTH_RE = re.compile(r"[​-‍﻿⁠]")
#: 出现即视为"这一行自己已经收口"的标点，全角与半角都要认（PDF 两种都会产出）。
#: 判据只用于**拼接**，不改写正文：行末有它就不再补分隔符，没有就补一个空格。
#: 为什么不再补"；"：PDF 的换行是版式换行不是语义换行，实测把"市场推广"拼成
#: "推；广"、"服务于本公司"拼成"服务于；本公司"，而 validate 的模型意见一半在报这个。
_SENTENCE_DELIMITERS = "。；，、！？：;,.!?:"


def join_wrapped_lines(lines: Iterable[str]) -> str:
    """把同一条目里被 PDF 拆开的若干行拼回去，返回拼接后的单行。

    行末已有分隔标点就直拼，没有只补一个空格：空格在 `squeeze()` 里会被抹掉，
    所以它不声称原文此处有过标点；补"；"则是替原文造一个不存在的话题边界。
    """
    parts = [collapse(line) for line in lines if collapse(line)]
    if not parts:
        return ""
    out = parts[0]
    for part in parts[1:]:
        separator = "" if out[-1] in _SENTENCE_DELIMITERS else " "
        out = f"{out}{separator}{part}"
    return out


@dataclass(frozen=True)
class Evidence:
    """一个抽出来的值，连带它出自原文哪一段。"""

    value: str
    evidence: str
    source: Source


def collapse(text: str) -> str:
    """把连续空白压成单个空格。

    PDF 为了对齐会插进几十个大空格，直接当 evidence 存下来既占地方又没法读。
    """
    return _WHITESPACE_RE.sub(" ", _ZERO_WIDTH_RE.sub("", text)).strip()


def squeeze(text: str) -> str:
    """删掉所有空白，用于证据归属核对：模型抄写时空格数不同不该被判成幻觉。"""
    return _WHITESPACE_RE.sub("", _ZERO_WIDTH_RE.sub("", text))


def evidence_belongs(raw_text: str, evidence: str) -> bool:
    """`evidence` 是否真的出自 `raw_text`（忽略空白与零宽字符）。"""
    needle = squeeze(evidence)
    return bool(needle) and needle in squeeze(raw_text)
