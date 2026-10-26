"""JD 要求项拆分与技能对齐：确定性的缺口判定〔阶段 7 · §10.4.3〕

本文件是 `match` 的**规则层**：不碰 LLM、不碰数据库、零成本、可单测。分工是——
- 能用词典判的（JD 点了技术名的那些要求）在这里判完；
- 判不了的（"较强的沟通能力"这类没有词典名字的软要求）打包成 `unresolved`，
  交给 `match_node` 的那一次 LLM 调用，再按证据铁律核对后回收。

为什么拆分不交给模型：JD 的要求项有稳定的排版记号（分号、顿号以外的逗号、序号、
"任职要求："这类栏目标题），正则切得干净，而且**同一条要求每次必须切成同一份**——
缺口清单一旦每次切法不同，下游 `profile` 的分数与 `plan` 的配额就全成了噪声。

`level` 的四档语义：
- `matched`：简历里有这项技能，程度不低于 JD 写的程度；
- `partial`：技能在，但程度写得更浅（或 JD 要"精通"而简历根本没写程度词）；
- `missing`：技能不在简历里；
- `unverified`：词典判不了、模型也没给出能核对上原文的证据——**留给下游但别当依据**，
  `profile_service` 的岗位匹配度只统计前三档。
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.extractors.base import collapse, evidence_belongs, squeeze
from app.extractors.skill_extractor import LEVEL_RANK, SkillMention, skill_mentions_in

GapLevel = Literal["matched", "partial", "missing", "unverified"]
SectionName = Literal["required", "preferred", "duty"]

#: 拆分点。顿号 `、` 刻意不在其中：`熟悉 MySQL、PostgreSQL 索引原理` 是一条要求里
#: 点了两个技能，切成两条会让 "PostgreSQL" 丢掉"熟悉"这个程度词，判档就偏了。
_SPLIT_RE = re.compile(r"[；;，,。：:\n\r]+")
#: 条目序号与项目符号（`1.` `（1）` `- ` `• `），去掉后剩下的才是要求本身
_BULLET_RE = re.compile(r"^\s*(?:[-*•·●]|\d+\s*[.、)）]|[（(]\s*\d+\s*[)）])\s*")

#: 栏目标题 -> 归属段落。命中即切换 section，标题本身不算要求项：
#: 它没有可核对的技术名，留着只会变成一条永久的 `unverified` 噪声。
_HEADINGS: tuple[tuple[str, SectionName], ...] = (
    (r"加分(?:项|要求)?|优先考虑|优先要求|nice\s?to\s?have", "preferred"),
    (r"岗位职责|工作(?:内容|职责)|职位描述|岗位描述|职责描述|responsibilities", "duty"),
    (
        r"(?:任职|岗位|职位|技能|其他)?(?:要求|条件|资格)|requirements?|qualifications",
        "required",
    ),
)
_HEADING_RES: tuple[tuple[re.Pattern[str], SectionName], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), section) for pattern, section in _HEADINGS
)

#: 短于这个长度的片段多半是"其他"、"优先"这类残句，不值得占一条缺口
_MIN_REQUIREMENT_CHARS = 4

#: 模型只许给这三档；`unverified` 是系统内部状态，不许模型自己声明。
#: 写成"输入档 -> GapLevel"的字典而不是枚举元组，是为了让档位收口在一处：
#: 表外的值一律当没声明过（按待核处理），而不是抛异常或原样穿过。
_JUDGED_LEVELS: dict[str, GapLevel] = {
    "matched": "matched",
    "partial": "partial",
    "missing": "missing",
}
#: 严重度排序：同一技能被多处要求时，留最严重的那条（缺 > 浅 > 有）
_SEVERITY: dict[str, int] = {"missing": 0, "partial": 1, "unverified": 2, "matched": 3}


@dataclass(frozen=True)
class Requirement:
    """一条 JD 要求。`text` 是 JD 原文里的逐字片段（空白折叠后），可回溯。"""

    text: str
    section: SectionName = "required"
    mentions: tuple[SkillMention, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "requirement": self.text,
            "section": self.section,
            "skills": [mention.name for mention in self.mentions],
        }


@dataclass(frozen=True)
class MatchResult:
    """规则层的产出：已定档的缺口 + 待模型判定的要求 + 给人看的说明。"""

    gaps: list[dict[str, Any]]
    unresolved: tuple[Requirement, ...]
    notes: tuple[str, ...]
    requirements: tuple[Requirement, ...]

    def gap_for(self, skill: str) -> dict[str, Any] | None:
        """按技能名找回缺口条目（大小写不敏感）。"""
        key = skill.strip().lower()
        return next((gap for gap in self.gaps if str(gap.get("skill") or "").lower() == key), None)

    def gap_level(self, skill: str) -> str | None:
        """某个技能的判档结果。测试与画像层用它，不必各自手写查找。"""
        gap = self.gap_for(skill)
        return str(gap["level"]) if gap is not None else None


def split_requirements(jd_text: str) -> tuple[Requirement, ...]:
    """把 JD 切成要求项。段落标题决定每条要求的 `section`，默认按 `required` 算。

    默认 required 而非 duty：一份没有栏目标题的 JD 通常整段都在提要求，
    把它当成职责会让缺口清单变成空表。
    """
    out: list[Requirement] = []
    seen: set[str] = set()
    section: SectionName = "required"
    for piece in _SPLIT_RE.split(jd_text):
        text = collapse(_BULLET_RE.sub(" ", piece))
        if not text:
            continue
        heading = _heading_section(text)
        if heading is not None:
            # 标题必须**先于**长度过滤判定：`加分项` 只有 3 字，若先过长度门槛就被当残句
            # 丢掉了，于是它下面那批"加分要求"会被错标成正经要求，权重全错
            section = heading
            continue
        if len(text) < _MIN_REQUIREMENT_CHARS:
            continue
        key = squeeze(text)
        if key in seen:
            continue
        seen.add(key)
        out.append(Requirement(text=text, section=section, mentions=tuple(skill_mentions_in(text))))
    return tuple(out)


def _heading_section(text: str) -> SectionName | None:
    for pattern, section in _HEADING_RES:
        if pattern.fullmatch(text):
            return section
    return None


def build_match(jd_text: str, parsed: Mapping[str, Any]) -> MatchResult:
    """规则层的完整一趟：拆要求 → 与 `parsed.skills` 对齐 → 每个技能一条缺口。

    缺口按技能去重（`matched_gaps` 是下游画像与配额的输入，一个技能出现两次会让
    岗位匹配度被同一件事反复加权），保留该技能严重度最高的那条。
    """
    skills = _skill_index(parsed)
    requirements = split_requirements(jd_text)
    notes: list[str] = []
    if not requirements:
        notes.append("JD 里没拆出任何要求项，缺口清单只能交给模型或人工兜底")

    gaps: dict[str, dict[str, Any]] = {}
    unresolved: list[Requirement] = []
    duty_count = 0
    for requirement in requirements:
        if requirement.section == "duty":
            duty_count += 1
            continue
        if not requirement.mentions:
            unresolved.append(requirement)
            continue
        for mention in requirement.mentions:
            gap = _align_one(requirement, mention, skills.get(mention.name.lower()) or {})
            _keep_worst(gaps, gap)

    if duty_count:
        notes.append(f"{duty_count} 条是岗位职责（不是技能要求），未计入缺口")
    if unresolved:
        notes.append(
            f"{len(unresolved)} 条要求没点到词典里的技术名，交给模型判定："
            + "；".join(item.text for item in unresolved[:3])
        )
    return MatchResult(
        gaps=list(gaps.values()), unresolved=tuple(unresolved), notes=tuple(notes),
        requirements=requirements,
    )


def _skill_index(parsed: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """`parsed.skills` 按小写技能名 indexing。重名已经在 `ResumeSchema` 合并过一轮。"""
    items = parsed.get("skills")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return {}
    index: dict[str, dict[str, Any]] = {}
    for item in items:
        if isinstance(item, Mapping) and isinstance(item.get("name"), str):
            index[item["name"].strip().lower()] = dict(item)
    return index


def _align_one(
    requirement: Requirement, mention: SkillMention, resume_skill: Mapping[str, Any]
) -> dict[str, Any]:
    """一条要求里的一个技能：缺 / 浅 / 有。"""
    if not resume_skill:
        return make_gap(
            requirement.text,
            "missing",
            skill=mention.name,
            required_level=mention.level,
            section=requirement.section,
        )
    resume_level = resume_skill.get("level")
    evidence = resume_skill.get("evidence")
    if mention.level is None:
        # JD 只说"有分布式经验优先"：简历提到就算数，程度词是 JD 自己没写
        level: GapLevel = "matched"
    # `LEVEL_RANK.get(..., -1)`：词典外的程度值按"最低档"算，宁可判 partial 不去 KeyError
    elif LEVEL_RANK.get(str(resume_level or ""), -1) < LEVEL_RANK[mention.level]:
        # 没写程度也是 partial：JD 要"精通"而简历只说"熟悉"，或缺程度词，证据都不够硬
        level = "partial"
    else:
        level = "matched"
    return make_gap(
        requirement.text,
        level,
        evidence=evidence if isinstance(evidence, str) else None,
        skill=mention.name,
        required_level=mention.level,
        resume_level=resume_level if isinstance(resume_level, str) else None,
        section=requirement.section,
    )


def make_gap(
    requirement: str,
    level: GapLevel,
    *,
    evidence: str | None = None,
    skill: str | None = None,
    required_level: str | None = None,
    resume_level: str | None = None,
    section: SectionName = "required",
    source: str = "lexicon",
) -> dict[str, Any]:
    """缺口条目的唯一构造点，保证 `matched_gaps` 每行形状一致。

    `evidence` 是简历原文片段（可 None）；`requirement` 是 JD 原文片段。两侧都能回溯，
    出题时 `generate` 才有的可引。`source` 记判定来源（词典还是模型），阶段 6 接上
    sqlite-vec 后还要多一档 `retrieval`——三种判据的召回率必须能分开归因。
    """
    return {
        "requirement": requirement,
        "level": level,
        "evidence": evidence,
        "skill": skill,
        "required_level": required_level,
        "resume_level": resume_level,
        "section": section,
        "source": source,
    }


def _keep_worst(gaps: dict[str, dict[str, Any]], candidate: dict[str, Any]) -> None:
    """按技能名留最严重的一条。没有技能名的条目（模型分支）不参与去重。"""
    key = str(candidate.get("skill") or "").lower()
    if not key:
        return
    previous = gaps.get(key)
    if previous is None or _SEVERITY[str(candidate["level"])] < _SEVERITY[str(previous["level"])]:
        gaps[key] = candidate


def adopt_llm_gaps(
    judgements: Iterable[Mapping[str, Any]],
    *,
    requirements: Sequence[Requirement],
    raw_text: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """回收模型对 `unresolved` 要求的判定。返回（可用缺口，丢弃说明）。

    三道闸，缺一不进：
    1. `requirement` 必须对得上某条待判定要求——模型自己新增的"岗位一般都要"一律不要；
    2. `matched` / `partial` 必须带证据，且证据能在简历原文里逐字找到（无证据即丢弃）；
    3. 证据核对不过的降级成 `unverified` 而不是直接删——要求本身确实出自 JD，
       删掉会让缺口清单谎称"这条要求不存在"。
    """
    gaps: list[dict[str, Any]] = []
    notes: list[str] = []
    for judgement in judgements:
        requirement = _match_requirement(str(judgement.get("requirement", "")), requirements)
        if requirement is None:
            notes.append(
                f"模型给出的要求不在待判定清单里，已丢弃：{str(judgement.get('requirement'))[:40]}"
            )
            continue
        short = requirement.text[:24]
        claimed = str(judgement.get("level", "unverified"))
        level: GapLevel = _JUDGED_LEVELS.get(claimed, "unverified")
        if level == "unverified" and claimed != "unverified":
            notes.append(f"要求「{short}」的判定档位 {claimed!r} 不在合法档里，按待核处理")
        evidence = judgement.get("evidence")
        text = evidence.strip() if isinstance(evidence, str) else None
        if level in ("matched", "partial") and (not text or not evidence_belongs(raw_text, text)):
            # 声称有证据却核对不上，等于没证据
            notes.append(f"要求「{short}」被判 {level}，但证据在简历原文里找不到，按待核处理")
            level, text = "unverified", None
        gaps.append(
            make_gap(
                requirement.text,
                level,
                evidence=text,
                section=requirement.section,
                source="llm",
            )
        )
    return gaps, notes


def _match_requirement(text: str, requirements: Sequence[Requirement]) -> Requirement | None:
    """把模型的转述对回待判定要求。先要完全一致，再接受"唯一包含关系"。"""
    needle = squeeze(text)
    if not needle:
        return None
    exact = next((item for item in requirements if squeeze(item.text) == needle), None)
    if exact is not None:
        return exact
    contained = [
        item
        for item in requirements
        if needle in squeeze(item.text) or squeeze(item.text) in needle
    ]
    # 只有一条能接上才算点名成功；两条都算得上就是模型在含糊其辞
    return contained[0] if len(contained) == 1 else None
