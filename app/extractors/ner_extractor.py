"""NER 实体抽取：正则打底 + LLM 兜底，输出带 evidence〔阶段 5 · §8.6.1〕

分工是按"能不能白拿"定的：手机 / 邮箱 / "N 年经验" 这类有稳定字形的东西，正则
一次就够，不必花 token；姓名、座机这种依赖版式与语义的才交给 LLM。真简历
`附件简历-梁清生.pdf` 里 `17626460250` 与 `3405628625@qq.com` 就是白拿的两项。

兜底那一路有硬规矩：**LLM 说值不算，必须给原文证据，且证据得在原文里找得到**。
证据核对不上的字段直接丢掉并记进 `notes`，宁可缺失也不留幻觉——这是
`app/schemas/resume.py` 里 evidence 字段存在的原因。

字段归属：本模块只负责 `ResumeSchema` 的联系人四项（name / email / phone /
years_experience）。技能见 `skill_extractor`，工作履历与年限复算见
`timeline_extractor`，项目见 `project_extractor`。
"""

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.extractors.base import Evidence, collapse, evidence_belongs
from app.prompts.registry import get_prompt
from app.utils.llm_client import LLMClient

#: 中国大陆手机号：11 位、1[3-9] 开头，允许 +86 前缀与空格/横线分隔。
#: 前后断言挡掉 18 位身份证——那玩意儿也以 1 开头，不挡就会误命中。
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[\s-]*)?1[3-9](?:[\s-]*\d){9}(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
#: 只认数字，"两年经验"这类汉字数值交给 LLM；必须跟着"经验/经历/从业"，
#: 免得把"3 年合同""2021-2025"读成工作年限。
_YEARS_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![\d.])(\d{1,2}(?:\.\d)?)\s*年(?:的)?\s*(?:实习|工作|从业|开发|项目)?经[验历]"),
    re.compile(r"(?:工作|从业|开发)\s*(\d{1,2}(?:\.\d)?)\s*年"),
)
#: 显式标签才认；"第一行是俩字"那种版式猜测留给 LLM
_NAME_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"姓\s*名\s*[:：]?\s*([一-龥]{2,4}(?:·[一-龥]{1,4})?)"),
    re.compile(r"(?i)\bname\b\s*[:：]\s*([A-Za-z一-龥][A-Za-z一-龥 .·]{1,40})"),
)

@dataclass(frozen=True)
class NerResult:
    """联系人四项的抽取结果。拿不到的是 None，由调用方决定怎么补或怎么报。"""

    name: Evidence | None = None
    email: Evidence | None = None
    phone: Evidence | None = None
    years_experience: Evidence | None = None
    #: 用到的 prompt 模板标记（"ner@v1"）；纯正则命中时为空串
    prompt_version: str = ""
    notes: tuple[str, ...] = ()
    #: 两层都试过后仍然没有的字段名
    missing: tuple[str, ...] = ()

    def as_resume_fields(self) -> dict[str, Any]:
        """拼成 `ResumeSchema` 的一部分。None 字段不写入，让 schema 判必填。"""
        out: dict[str, Any] = {}
        for key in ("name", "email", "phone"):
            found = getattr(self, key)
            if found is not None:
                out[key] = found.value
        if self.years_experience is not None:
            out["years_experience"] = float(self.years_experience.value)
        return out


class _Span(BaseModel):
    """LLM 给的"值 + 原文证据"对。"""

    value: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class _NerPayload(BaseModel):
    """兜底接口的返回形状；没把握的字段模型应当填 null。"""

    name: _Span | None = None
    email: _Span | None = None
    phone: _Span | None = None
    years_experience: _Span | None = None


#: 字段名 -> 怎么从 LLM payload 里取那一项
_PULLERS: dict[str, Callable[[_NerPayload], _Span | None]] = {
    "name": lambda payload: payload.name,
    "email": lambda payload: payload.email,
    "phone": lambda payload: payload.phone,
    "years_experience": lambda payload: payload.years_experience,
}


def extract_by_regex(raw_text: str) -> NerResult:
    """不花钱的第一层。命中几个算几个，缺的交给 LLM。"""
    return NerResult(
        name=_first(_NAME_RES, raw_text, _group(1)),
        email=_first((_EMAIL_RE,), raw_text),
        phone=_first((_PHONE_RE,), raw_text, _digits),
        years_experience=_first(_YEARS_RES, raw_text, _group(1)),
    )


class NerExtractor:
    """正则打底，LLM 补漏，每个值都要求原文证据。"""

    def __init__(self, llm: LLMClient | None = None, settings: Settings | None = None) -> None:
        self.settings = settings or (llm.settings if llm is not None else get_settings())
        self._llm = llm or LLMClient(self.settings)

    async def extract(self, raw_text: str) -> NerResult:
        base = extract_by_regex(raw_text)
        missing = [key for key in _PULLERS if getattr(base, key) is None]
        if not missing:
            return base

        prompt = get_prompt("ner")
        payload = await self._llm.chat_json(
            "extract",
            prompt.render(missing="、".join(missing), raw_text=raw_text),
            schema=_NerPayload,
            temperature=0.0,
        )
        return _merge(base, _accept(payload, raw_text), missing, f"{prompt.node}@{prompt.version}")


def _accept(payload: _NerPayload, raw_text: str) -> dict[str, Evidence]:
    """把 LLM 的 span 转成 `Evidence`，核对不上的字段当成没给。

    核对交给 `evidence_belongs`：它先把空白与零宽字符清掉再比。原文里的对齐空格是
    PDF 排版残留，模型抄写时十个空写成两个，不该判成幻觉。
    """
    accepted: dict[str, Evidence] = {}
    for key, puller in _PULLERS.items():
        span = puller(payload)
        if span is None:
            continue
        value = span.value.strip()
        evidence = collapse(span.evidence)
        if not value or not evidence_belongs(raw_text, evidence):
            continue
        accepted[key] = Evidence(value=value, evidence=evidence, source="llm")
    return accepted


def _merge(
    base: NerResult, filled: dict[str, Evidence], missing: Sequence[str], version: str
) -> NerResult:
    updates: dict[str, Any] = dict(vars(base))
    updates["prompt_version"] = version
    notes = list(base.notes)
    still_missing = list(missing)
    for key in missing:
        gained = filled.get(key)
        if gained is None:
            notes.append(f"LLM 没给出可核对的 {key}，该字段留空")
            continue
        updates[key] = gained
        still_missing.remove(key)
    updates["notes"] = tuple(notes)
    updates["missing"] = tuple(still_missing)
    return NerResult(**updates)


def _whole(match: re.Match[str]) -> str:
    return match.group()


def _group(index: int) -> Callable[[re.Match[str]], str]:
    return lambda match: match.group(index).strip()


def _digits(match: re.Match[str]) -> str:
    digits = "".join(ch for ch in match.group() if ch.isdigit())
    # 国家码不是号码的一部分：`+86 138-0013-8000` 抽出来就该是 11 位
    if digits.startswith("86") and len(digits) > 11:
        return digits[2:]
    return digits


def _first(
    patterns: Iterable[re.Pattern[str]],
    raw_text: str,
    pull: Callable[[re.Match[str]], str] = _whole,
) -> Evidence | None:
    """按顺序取第一个命中的模式，返回"值 + 原文片段"。"""
    for pattern in patterns:
        match = pattern.search(raw_text)
        if match is not None:
            return Evidence(value=pull(match), evidence=collapse(match.group()), source="regex")
    return None
