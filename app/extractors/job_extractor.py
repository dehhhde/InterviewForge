"""岗位字段抽取：从 JD 原文认 title / company / min_years〔写入侧 · 图片录入岗位〕

分工照 `ner_extractor` 的老规矩定：**能不能白拿**。岗位名和公司名在中文 JD 里绝大多数带显式
标签（"岗位名称：""【职位】"、落款公司后缀），年限写成"3-5 年经验"这类稳定字形，三者都不必
花 token；只有岗位名或公司名一条都没捞到时，才让 LLM 补那两项。

`min_years` 不走 LLM 兜底：AGENTS.md §7 记着实测——模型连"哪个日期更晚"都会算错，
"3-5 年取下界"这种事必须留在规则层。规则认不出就是 0（页面显示"不限"，人自己填）。

兜底那一路与简历侧同一条铁律：值必须能在 `jd_text` 里逐字找到出处。图片录入场景这条尤其要紧
——OCR 本身就会糊字，再让模型"顺手补全"公司名，等于把幻觉二次加工后写进库。
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.extractors.base import Evidence, collapse, evidence_belongs, squeeze
from app.prompts.registry import get_prompt
from app.utils.llm_client import LLMClient

#: 标签式岗位名。备选项必须**长的在前**：`岗位` 排在 `岗位名称` 之前会先吃掉前两字，
#: 剩下的 `名称：` 让冒号断言失败，整行就漏了。
_TITLE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:岗位名称|职位名称|招聘岗位|应聘岗位|招聘职务|岗位|职位|职务)\s*[:：]\s*([^\s:：][^\n]{0,79})"
    ),
    re.compile(r"[【\[]\s*(?:岗位名称|职位名称|岗位|职位|职务)\s*[】\]]\s*([^\n]{1,80})"),
)
#: 无标签版式（招聘海报最常见）：开头的短句里带职位词才算岗位名。
#: 英文词只在标题行与年限行了，OCR 出来的海外 JD 也走这一条路。
#: `re.IGNORECASE` 只为英文：中文字符不受它影响，而 OCR 常把大小写糊掉。
_ROLE_KEYWORD_RE = re.compile(
    r"工程师|开发|架构师|设计师|测试|运维|运营|产品|数据分析|算法|前端|后端|全栈"
    r"|实习|顾问|专家|经理|主管|总监|研究员|程序员"
    r"|\b(?:engineer|developer|architect|designer|analyst|scientist|consultant|manager|intern)\b",
    re.IGNORECASE,
)
#: 标签后面的值如果其实是下一段的小标题（"岗位职责：负责……"被 OCR 串行），不算岗位名
_TITLE_STOPWORD_RE = re.compile(r"职责|要求|描述|说明|待遇|福利|须知|内容")
#: 海报标题里常夹地点/编制："【高级后端工程师】Base 上海"，括号后的部分是补充说明
_TITLE_CUT_RE = re.compile(r"[】\]（(【\[]")

#: 落款公司。后缀词典刻意不含裸 `公司`——"公司介绍："这类标题会被咬住。
_COMPANY_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:公司名称|招聘单位|招聘企业|公司|企业|单位)\s*[:：]\s*([^\s:：][^\n]{0,59})"),
    re.compile(
        r"([一-龥A-Za-z0-9（）()·&]{2,39}?(?:股份有限公司|有限公司|集团|研究院|研究所|事务所))"
    ),
    re.compile(
        r"([A-Za-z][A-Za-z0-9&., \-]{2,59}?(?:Ltd\.|Inc\.|LLC|Co\., ?Ltd\.|Corporation))", re.ASCII
    ),
)

#: 年限必须待在这一行里才算，否则"成立于 2015 年""3 年合同期"都会变成期望年限。
_YEARS_CONTEXT_RE = re.compile(
    r"经验|年限|资历|从业|\b(?:experience|working\s+years?|yoe)\b", re.IGNORECASE
)
#: 区间在前：`3-5 年` 若先过裸匹配会只认出 5 年（`年` 咬的是后一个数）
_YEARS_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![\d.])(\d{1,2}(?:\.\d)?)\s*[-~～至到]\s*(?:\d{1,2}(?:\.\d)?)\s*年"),
    re.compile(r"(?<![\d.])(\d{1,2}(?:\.\d)?)\s*年(?:及)?(?:以上|起)"),
    re.compile(r"(?<![\d.])(\d{1,2}(?:\.\d)?)\s*\+?\s*years?", re.IGNORECASE),
    re.compile(r"(?<![\d.])(\d{1,2}(?:\.\d)?)\s*年(?:的)?"),
)

#: `Job.min_years` 的合法区间（与 `JobCreateRequest` 的 ge/le 同源），越界当误命中跳过
_MIN_YEARS = 0.5
_MAX_YEARS = 60.0

#: 值尾部残留的行文标点：OCR 会把整行照抄，"后端工程师。"进了自然键就是另一个岗位
_TRAILING_PUNCT = "。，,；;、．·-—()（）【】[] \t"

#: 中文标签 -> 提示语里给页面看的字段名
_FIELD_LABELS = {"title": "岗位名称", "company": "公司名称"}

#: 交给 LLM 的只有这两项：`min_years` 是数字判断，不交给模型（见模块 docstring）
_LLM_FIELDS: tuple[str, ...] = ("title", "company")


@dataclass(frozen=True)
class JobFieldResult:
    """岗位三字段的抽取结果。拿不到的是 `None`，怎么提示留给服务层与页面。"""

    title: Evidence | None = None
    company: Evidence | None = None
    #: `Evidence.value` 存数字字符串，转 float 由调用方做（与 `years_experience` 一致）
    min_years: Evidence | None = None
    #: 用到的 prompt 模板标记（"job_fields@v1"）；纯正则命中时为空串
    prompt_version: str = ""
    notes: tuple[str, ...] = ()
    #: 两层都试过后仍然没有的字段名
    missing: tuple[str, ...] = ()


class _JobFieldSpan(BaseModel):
    """LLM 给的"值 + 原文证据"对。"""

    value: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class _JobFieldsPayload(BaseModel):
    """兜底接口的返回形状；没把握的字段模型应当填 null。"""

    title: _JobFieldSpan | None = None
    company: _JobFieldSpan | None = None


def extract_by_rules(jd_text: str) -> JobFieldResult:
    """不花钱的第一层。命中几个算几个，缺的交给 LLM 兜底。"""
    return JobFieldResult(
        title=_extract_title(jd_text),
        company=_first(_COMPANY_RES, jd_text),
        min_years=_extract_min_years(jd_text),
    )


class JobFieldExtractor:
    """规则打底，LLM 补岗位名/公司名的漏，每个值都要有原文出处。"""

    def __init__(self, llm: LLMClient | None = None, settings: Settings | None = None) -> None:
        self.settings = settings or (llm.settings if llm is not None else get_settings())
        self._llm = llm or LLMClient(self.settings)

    async def extract(self, jd_text: str) -> JobFieldResult:
        base = extract_by_rules(jd_text)
        missing = [key for key in _LLM_FIELDS if getattr(base, key) is None]
        if not missing:
            return base

        prompt = get_prompt("job_fields")
        payload = await self._llm.chat_json(
            "extract",
            prompt.render(missing="、".join(missing), jd_text=jd_text),
            schema=_JobFieldsPayload,
            temperature=0.0,
        )
        return _merge(base, _accept(payload, jd_text), missing, f"{prompt.node}@{prompt.version}")


def _merge(
    base: JobFieldResult, filled: dict[str, Evidence], missing: Sequence[str], version: str
) -> JobFieldResult:
    """把兜底认回来的字段并回规则结果，剩下的记进 `notes` 与 `missing`。"""
    updates: dict[str, Any] = dict(vars(base))
    updates["prompt_version"] = version
    notes = list(base.notes)
    still_missing = list(missing)
    for key in missing:
        gained = filled.get(key)
        if gained is None:
            notes.append(f"{_FIELD_LABELS[key]}认不出出处，留空待人工填写")
            continue
        updates[key] = gained
        still_missing.remove(key)
    updates["notes"] = tuple(notes)
    updates["missing"] = tuple(still_missing)
    return JobFieldResult(**updates)


def _accept(payload: _JobFieldsPayload, jd_text: str) -> dict[str, Evidence]:
    """把 LLM 的 span 转成 `Evidence`，核对不上的字段当成没给。

    与简历侧多核一层：不光证据要在原文里，**值还得就含在证据里**。岗位名是这张表的自然键，
    模型若把证据抄成"高级后端工程师"却把值写成"后端开发工程师"，那是换了个岗位，
    只比证据会放过去。
    """
    accepted: dict[str, Evidence] = {}
    for key in _LLM_FIELDS:
        span = getattr(payload, key)
        if span is None:
            continue
        value = collapse(span.value).strip(_TRAILING_PUNCT)
        evidence = collapse(span.evidence)
        if not value or not evidence_belongs(jd_text, evidence):
            continue
        if squeeze(value) not in squeeze(evidence):
            continue
        accepted[key] = Evidence(value=value, evidence=evidence, source="llm")
    return accepted


def _extract_title(jd_text: str) -> Evidence | None:
    """标签命中优先，其次开头短句的职位词；两者都要过 `_title_ok`。"""
    labeled = _first(_TITLE_RES, jd_text, accept=_title_ok)
    if labeled is not None:
        return labeled
    return _title_from_heading(jd_text)


def _title_from_heading(jd_text: str) -> Evidence | None:
    """招聘海报版式：大标题一行就是岗位名，没有任何标签（"【高级后端工程师】Base 上海"）。

    只看前 3 个非空行，再往下的短句多半已经是"岗位职责"里的句子了。
    先剥开括号再在闭括号处切：`_TITLE_CUT_RE` 的第一个命中往往是**开**括号，
    不先剥就直接 split 会得到空串；而 `_clean` 只去首尾标点，行中的 `】Base 上海` 得靠切。
    """
    for line in _non_empty_lines(jd_text)[:3]:
        heading = _clean(_TITLE_CUT_RE.split(line.strip("【[（("), maxsplit=1)[0])
        if not heading or not _title_ok(heading):
            continue
        if len(heading) > 40 or ":" in heading or "：" in heading:
            continue
        # "1. 负责数据平台开发" 也是短句、也带"开发"，但它是列表项不是标题
        if heading[0].isdigit() or heading[0] in "-•*·、。":
            continue
        if _ROLE_KEYWORD_RE.search(heading):
            return Evidence(value=heading, evidence=collapse(line), source="regex")
    return None


def _title_ok(value: str) -> bool:
    """挡掉"岗位名称：\n岗位职责"这类值本身是小标题的命中。"""
    return not _TITLE_STOPWORD_RE.search(value)


def _extract_min_years(jd_text: str) -> Evidence | None:
    """逐行找年限，区间取**下界**（"3-5 年"说的是"至少 3 年"）。

    必须落在带"经验/年限"的同一行：整段匹配会让"5 年经验"和另一段的"2019 年毕业"互相认亲。
    """
    for line in jd_text.splitlines():
        if not _YEARS_CONTEXT_RE.search(line):
            continue
        for pattern in _YEARS_RES:
            match = pattern.search(line)
            if match is None:
                continue
            years = float(match.group(1))
            if _MIN_YEARS <= years <= _MAX_YEARS:
                span = collapse(match.group())
                return Evidence(value=match.group(1), evidence=span, source="regex")
            # 越界（"60 年以上"多半是 OCR 把薪资糊进了年限行）就换下一条线，别硬填
            break
    return None


def _first(
    patterns: Sequence[re.Pattern[str]],
    jd_text: str,
    accept: Callable[[str], bool] | None = None,
) -> Evidence | None:
    """按顺序取第一个"值合格"的命中：值取捕获组 1，证据取整个匹配。"""
    for pattern in patterns:
        for match in pattern.finditer(jd_text):
            value = _clean(match.group(1))
            if value and (accept is None or accept(value)):
                return Evidence(value=value, evidence=collapse(match.group()), source="regex")
    return None


def _clean(value: str) -> str:
    """压空白 + 去尾部行文标点：自然键里多一个句号就是另一个岗位。"""
    return collapse(value).strip(_TRAILING_PUNCT)


def _non_empty_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]
