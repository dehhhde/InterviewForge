"""工作时间线抽取：区间解析 + 合并重叠 + 年限计算〔阶段 5 · §8.6.4〕

教育经历也归这里：中文简历里两者是同一种字形——`名称 [+ 学历/专业] + 大段空格 + 日期区间`。
拆开写等于把同一套行解析抄两遍，而"年限与时间线是否自洽"这条校验本来就要同时看两段。

年限是**算出来的**，不是抄出来的，所以它的 `evidence` 记的是所依据的区间原文。
区间倒挂（结束早于开始）不硬凑：丢掉该段，同时记进 `notes`（给人看的流水账）和
`dropped`（给 M2 校验层机器判定的结构化记录）。节点把 notes 并进只追加的 `errors` 通道。
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from app.extractors.base import Evidence, collapse, join_wrapped_lines
from app.extractors.sections import SectionMap, split_sections
from app.schemas.resume import EducationItem, WorkExperience

#: `2024.06-2026.06` / `2021-2025` / `2024/6~2025/7` / `2024年6月-至今` / `2021年-现在`
#: 起始月份必须整体可选，否则 `2021-2025` 里那个连字符会被当成"年月分隔符"吃掉，
#: 再找第二个连字符就匹配不上——只有年份的区间恰恰是最常见的一种写法。
#: 末尾那个 `|\s*年` 是给 `2021年-现在` 准备的：没有月份时"年"就是纯后缀。
_RANGE_RE = re.compile(
    r"(?<![\d.])(?P<sy>\d{4})(?:\s*[.\-/年]\s*(?P<sm>\d{1,2})\s*月?|\s*年)?\s*"
    r"(?:[-—–~]|至|到)\s*"
    r"(?:(?P<ey>\d{4})(?:\s*[.\-/年]\s*(?P<em>\d{1,2})\s*月?|\s*年)?|至今|今|现在|now|present)",
    re.IGNORECASE,
)
#: 版式留给对齐的大空格，那就是字段边界
_GAP_RE = re.compile(r"\s{2,}|[|｜]")
_DAYS_PER_YEAR = 365.25
#: 出现在某个分段里就判它是学历，否则那个分段当专业。
#: 拉丁字样（MBA/EMBA）只走 `_LATIN_DEGREE_RE` 的词边界判据，中文字样才用子串。
DEGREE_WORDS = (
    "大专",
    "专科",
    "本科",
    "学士",
    "硕士",
    "研究生",
    "博士",
    "MBA",
    "EMBA",
    "中专",
    "高中",
)
#: 大小写不敏感，但两端必须是词边界：简历里 `MBA` 常挨着标点或空格，
#: 而 `ABMATIC` 这类单词里含 `MBA` 不算学历（子串判据在拉丁字母上太松）。
_LATIN_DEGREE_RE = re.compile(r"\b(EMBA|MBA)\b", re.IGNORECASE)


@dataclass(frozen=True)
class DateRange:
    """一行里解析出来的日期区间。`end is None` 表示"至今"，与"没抽到日期"是两回事。"""

    start: date
    end: date | None
    text: str
    #: 区间之前的文字，通常是 `公司 职位` 或 `学校 学历 专业`
    head: str


#: 一条带日期的行为什么没进 `parsed`。M2 的"日期不倒挂"规则要靠它判定——
#: 倒挂条目进不了 `ResumeSchema`（`_check_date_order` 会 raise），所以只能这样传下去。
DropReason = Literal["inverted", "no_company", "no_school", "invalid"]


@dataclass(frozen=True)
class DroppedEntry:
    reason: DropReason
    #: 整行原文（空白折叠过），校验层拿它当证据
    line: str
    #: 区间原文，形如 `2023.05-2022.01`
    range_text: str
    start: date
    end: date | None
    #: 丢弃时写给 `notes` 的那句话，节点直接拿来进 `errors`
    note: str


@dataclass(frozen=True)
class TimelineResult:
    work_experiences: list[WorkExperience]
    educations: list[EducationItem]
    years_experience: Evidence | None
    notes: tuple[str, ...]
    #: 认出了日期区间但没进 `parsed` 的行（原因见 `DropReason`）
    dropped: tuple[DroppedEntry, ...] = ()

    def as_resume_fields(self) -> dict[str, object]:
        out: dict[str, object] = {
            "work_experiences": [i.model_dump(mode="json") for i in self.work_experiences],
            "educations": [i.model_dump(mode="json") for i in self.educations],
        }
        if self.years_experience is not None:
            out["years_experience"] = float(self.years_experience.value)
        return out


def parse_range(line: str) -> DateRange | None:
    """取一行里第一个日期区间。只有年份时按年初/年末补齐——简历不会写月份，
    那就不该假装知道，误差留给"年限"这种粗粒度字段。"""
    match = _RANGE_RE.search(line)
    if match is None:
        return None
    start = _to_date(int(match["sy"]), match["sm"], end_of_period=False)
    end_text = match["ey"]
    end = None if end_text is None else _to_date(int(end_text), match["em"], end_of_period=True)
    return DateRange(
        start=start,
        end=end,
        text=collapse(match.group()),
        head=line[: match.start()].strip(),
    )


@dataclass
class _Scratch:
    """抽取过程中的两类副产物：给用户看的 `notes` 与给校验层看的 `dropped`。

    同一次丢弃既要出现在流水账里（"我扔了什么"），又要能被 M2 机器判定，
    所以一处调用同时写两份，别让以后改文案的人忘了同步。
    """

    notes: list[str] = field(default_factory=list)
    dropped: list[DroppedEntry] = field(default_factory=list)

    def note(self, text: str) -> None:
        self.notes.append(text)

    def drop(self, reason: DropReason, found: DateRange, line: str, text: str) -> None:
        self.notes.append(text)
        self.dropped.append(
            DroppedEntry(
                reason=reason,
                line=collapse(line),
                range_text=found.text,
                start=found.start,
                end=found.end,
                note=text,
            )
        )


def extract_timeline(
    raw_text: str, *, today: date | None = None, sections: SectionMap | None = None
) -> TimelineResult:
    """按栏目取带日期的行，切成工作/教育条目，再用工作区间算总年限。

    `sections` 是给调用方的省钱入口（T-208）：`split_sections` 是纯正则遍历，一次 parse
    要喂三个抽取器，validate 回退时还要再喂一遍。省略则自切。传进来的必须是从同一份
    `raw_text` 切出的表——栏目归属由文本决定，两条路径的产出逐字节相同。
    """
    resolved_today = today or date.today()
    resolved_sections = sections if sections is not None else split_sections(raw_text)
    scratch = _Scratch(notes=list(_header_notes(resolved_sections)))

    works, work_ranges = _collect_work(resolved_sections.lines_of("work"), scratch)
    if resolved_sections.lines_of("work") and not work_ranges:
        scratch.note("工作经历栏目里一个日期区间都没认出来，年限只能靠「经验」字样那类原文线索")
    educations = _collect_educations(resolved_sections.lines_of("education"), scratch)
    years = _years_from_ranges(work_ranges, resolved_today, scratch)
    return TimelineResult(
        work_experiences=works,
        educations=educations,
        years_experience=years,
        notes=tuple(scratch.notes),
        dropped=tuple(scratch.dropped),
    )


def _collect_work(
    lines: Sequence[str], scratch: _Scratch
) -> tuple[list[WorkExperience], list[DateRange]]:
    """工作段：日期行开一段，其后直到下一个日期行的正文都算这段的职责描述。"""
    items: list[WorkExperience] = []
    ranges: list[DateRange] = []
    pending: WorkExperience | None = None
    bullets: list[str] = []

    for line in lines:
        found = parse_range(line)
        if found is not None:
            _close(items, pending, bullets)
            pending, bullets = None, []
            entry = _work_item(found, line, scratch)
            if entry is None:
                continue
            pending, bullets = entry, []
            ranges.append(found)
            continue
        if pending is not None:
            bullets.append(collapse(line))

    _close(items, pending, bullets)
    return items, ranges


def _fields(head: str) -> list[str]:
    """把日期区间前面那截切成字段。

    PDF 用大空格对齐栏目，所以先按 2 个以上空格（或竖线）切；只切出一段时退回单空格
    ——DOCX 和手打的简历就是一个空格。代价是英文公司名里带空格会被切开
    （"Google DeepMind 研究" 变成公司=Google、职位=DeepMind），中文简历里这种写法少见。
    """
    parts = [collapse(part) for part in _GAP_RE.split(head) if collapse(part)]
    if len(parts) > 1:
        return parts
    return [part for part in head.split() if part]


def _work_item(found: DateRange, line: str, scratch: _Scratch) -> WorkExperience | None:
    parts = _fields(found.head)
    if not parts:
        scratch.drop("no_company", found, line, f"日期区间前面没有公司名：{collapse(line)[:40]}")
        return None
    if found.end is not None and found.end < found.start:
        # 不硬凑成"看起来对"的区间：倒挂本身就是要报给校验层的事实
        scratch.drop("inverted", found, line, f"工作区间倒挂，已丢弃：{found.text}")
        return None
    if len(parts) < 2:
        scratch.note(f"没认出职位，只取公司名：{parts[0][:40]}")
    return WorkExperience(
        company=parts[0],
        role=parts[1] if len(parts) > 1 else None,
        start=found.start,
        end=found.end,
        evidence=collapse(line),
    )


def _collect_educations(lines: Sequence[str], scratch: _Scratch) -> list[EducationItem]:
    items: list[EducationItem] = []
    for line in lines:
        found = parse_range(line)
        if found is None:
            continue
        parts = _fields(found.head)
        if not parts:
            scratch.drop(
                "no_school", found, line, f"教育条目没有学校名，已丢弃：{collapse(line)[:40]}"
            )
            continue
        if found.end is not None and found.end < found.start:
            scratch.drop("inverted", found, line, f"教育区间倒挂，已丢弃：{found.text}")
            continue
        degree: str | None = None
        major: str | None = None
        for part in parts[1:]:
            word = _degree_word(part)
            if word is not None:
                degree = degree or word
            elif major is None:
                major = part
        try:
            items.append(
                EducationItem(
                    school=parts[0],
                    degree=degree or "other",
                    major=major,
                    start_year=found.start.year,
                    graduate_year=(found.end or found.start).year,
                    evidence=collapse(line),
                )
            )
        except ValueError as exc:
            scratch.drop("invalid", found, line, f"教育条目不合格，已丢弃：{exc}")
    return items


def _years_from_ranges(
    ranges: Sequence[DateRange], today: date, scratch: _Scratch
) -> Evidence | None:
    """合并重叠区间再求和：实习与全职叠在一起不该被算两遍。"""
    if not ranges:
        return None
    spans = sorted((r.start, r.end or today) for r in ranges)
    merged: list[list[date]] = []
    overlaps = 0
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            overlaps += 1
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    if overlaps:
        scratch.note(f"有 {overlaps} 段时间线重叠，已合并成一段计（可能有一段是实习或兼职）")

    days = sum(max(0, (end - start).days) for start, end in merged)
    return Evidence(
        value=str(round(days / _DAYS_PER_YEAR, 1)),
        evidence=" + ".join(r.text for r in ranges),
        source="regex",
    )


def _header_notes(sections: SectionMap) -> list[str]:
    notes: list[str] = []
    if sections.unknown_headers:
        notes.append(
            "这些短行像栏目名但不在词典里，相应内容可能整段漏抽："
            f"{'、'.join(sections.unknown_headers[:5])}"
        )
    if not sections.sections:
        notes.append("一个栏目都没认出来，时间与技能只能从整篇按行硬找")
    return notes


def _degree_word(part: str) -> str | None:
    """分段里出现的学历字样（"本科学士"取先命中的那个），没有就当它是专业。

    拉丁字样先过词边界判据：命中后统一成大写，`mba` 与 `MBA` 是同一种学历，
    大小写不该决定它进不进别名表（`schemas/resume.py` 的 `DEGREE_ALIASES` 按大写登记）。
    """
    latin = _LATIN_DEGREE_RE.search(part)
    if latin is not None:
        return latin.group(1).upper()
    return next((word for word in DEGREE_WORDS if word in part), None)


def _close(items: list[WorkExperience], pending: WorkExperience | None, bullets: list[str]) -> None:
    if pending is None:
        return
    # 这些行是"日期行之后直到下一个日期行"的正文，PDF 的换行会把一句职责劈成两半，
    # 所以按换行连接判据拼（`join_wrapped_lines`），不再用"；"假装它们是并列条目
    summary = join_wrapped_lines(bullets) or None
    items.append(
        pending if summary is None else pending.model_copy(update={"summary": summary})
    )


def _to_date(year: int, month: str | None, *, end_of_period: bool) -> date:
    if month is None:
        return date(year, 12, 31) if end_of_period else date(year, 1, 1)
    number = min(12, max(1, int(month)))
    if not end_of_period:
        return date(year, number, 1)
    next_month = date(year + (number == 12), 1 if number == 12 else number + 1, 1)
    return date.fromordinal(next_month.toordinal() - 1)
