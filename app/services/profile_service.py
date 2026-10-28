"""候选人画像规则层：五维 rubric 打分 + 弱项优先的主题清单〔阶段 7 · §10.4.4〕

本文件是 `profile` 的**打分层**：纯函数、零成本、可单测。`profile_node` 只负责
把它塞进通道并补一段被闸门过滤过的模型叙述。

为什么分数不交给模型：`plan` 必须确定性（计划书 M4），而它的输入就是这里的分数——
打分一旦带随机性，配额表就跟着抖，`review` 环节也就无法判断"这题为什么这么难"。
模型的力气因此放在叙述上（优势/劣势的措辞），那部分坏了可以退回规则拼出来的句子。

五条维度的取值域都是 1–5，`evidence` 是给**人**看的依据句：既写了算出来的数字，
也引用了简历原文片段，出题时能直接追溯"为什么要问这个"。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.extractors.base import collapse
from app.schemas.resume import LEVEL_RANK

DimensionName = Literal[
    "skill_breadth", "skill_depth", "experience", "job_fit", "project_evidence"
]

#: rubric 表版本：换阈值 = 换版本，`profile` 里带上它，"哪版评分变差了"才能归因
RUBRIC_VERSION = "v1"

#: 维度顺序 = 打分顺序 = 平局时的排序顺序。写死成元组，别用 dict 的字面顺序赌运气
_DIMENSIONS: tuple[tuple[DimensionName, str], ...] = (
    ("skill_breadth", "技能广度"),
    ("skill_depth", "技能深度"),
    ("experience", "经验年限"),
    ("job_fit", "岗位匹配度"),
    ("project_evidence", "项目实证"),
)
LABELS: dict[str, str] = dict(_DIMENSIONS)

#: 得分低于这条线的维度算"弱项"，进 `focus_topics` 且 `plan` 会给它保底槽位
WEAK_SCORE = 2

#: 主题清单上限：再多下游 `plan` 每槽摊不到一题，等于把弱项稀释
MAX_FOCUS_TOPICS = 8

#: 阈值阶梯 `(下限, 得分)`，按从大到小排。集中在这儿是为了让"改rubric"变成改一张表
#: 而不是改五处 if；分数只由输入量决定，与"今天是谁在看"无关
_BREADTH_STEPS: tuple[tuple[int, int], ...] = ((15, 5), (10, 4), (6, 3), (3, 2), (0, 1))
_YEARS_STEPS: tuple[tuple[float, int], ...] = ((8.0, 5), (5.0, 4), (3.0, 3), (1.0, 2), (0.0, 1))
#: 岗位命中率的算法：matched 记 1 分、partial 记 0.5 分，`unverified` 不进分母
_FIT_STEPS: tuple[tuple[float, int], ...] = (
    (0.9, 5),
    (0.7, 4),
    (0.5, 3),
    (0.25, 2),
    (0.0, 1),
)


@dataclass(frozen=True)
class Dimension:
    """一个维度的评分结果。`signals` 是给机器看的原始计数，`evidence` 是给人看的句子。"""

    name: DimensionName
    label: str
    score: int
    evidence: str
    signals: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "score": self.score,
            "evidence": self.evidence,
            "signals": dict(self.signals),
        }


def score_dimensions(
    parsed: Mapping[str, Any], gaps: Sequence[Mapping[str, Any]]
) -> list[Dimension]:
    """按 rubric 给五个维度打分。缺输入不抛错，按最低档给并在依据句里说明。"""
    skills = _as_mappings(parsed.get("skills"))
    return [
        _skill_breadth(skills),
        _skill_depth(skills),
        _experience(parsed.get("years_experience")),
        _job_fit(gaps),
        _project_evidence(_as_mappings(parsed.get("projects"))),
    ]


def _skill_breadth(skills: Sequence[Mapping[str, Any]]) -> Dimension:
    named = [str(item.get("name") or "").strip() for item in skills]
    count = len([name for name in named if name])
    score = _stepped(count, _BREADTH_STEPS)
    sample = "、".join(name for name in named if name)[:40]
    evidence = f"简历里认出 {count} 项技能：{sample}" if count else "简历里没认出任何技能条目"
    return _dimension("skill_breadth", score, evidence, {"skill_count": count})


def _skill_depth(skills: Sequence[Mapping[str, Any]]) -> Dimension:
    ranks = [LEVEL_RANK.get(str(item.get("level") or ""), -1) for item in skills]
    proficient = sum(1 for rank in ranks if rank >= 2)
    familiar = sum(1 for rank in ranks if rank == 1)
    if proficient >= 3:
        score = 5
    elif proficient >= 1 and familiar >= 3:
        score = 4
    elif proficient >= 1 or familiar >= 3:
        score = 3
    elif familiar >= 1:
        score = 2
    else:
        score = 1
    quote = next(
        (
            str(item.get("evidence") or "")
            for item in skills
            if str(item.get("level") or "") == "proficient"
        ),
        "",
    )
    evidence = f"自述精通 {proficient} 项、熟悉 {familiar} 项"
    if quote:
        evidence += f"，最硬的证据是「{collapse(quote)[:40]}」"
    if proficient == 0 and familiar == 0:
        evidence = "没有任何程度词，技能深度全靠简历条目数量撑着"
    return _dimension(
        "skill_depth", score, evidence, {"proficient": proficient, "familiar": familiar}
    )


def _experience(years: Any) -> Dimension:
    value = float(years) if isinstance(years, (int, float)) else 0.0
    score = _stepped(value, _YEARS_STEPS)
    evidence = (
        f"按日期区间复算的工作年限是 {round(value, 1)} 年"
        if value > 0
        else "没有可用的工作年限，默认按应届/无经验档"
    )
    return _dimension("experience", score, evidence, {"years_experience": round(value, 1)})


def _job_fit(gaps: Sequence[Mapping[str, Any]]) -> Dimension:
    decided = [gap for gap in gaps if str(gap.get("level")) in ("matched", "partial", "missing")]
    unverified = sum(1 for gap in gaps if str(gap.get("level")) == "unverified")
    if not decided:
        # 没有可比对的要求项时给中档，不给高分：高分会让 plan 以为这个方向不用担心
        return _dimension(
            "job_fit",
            3,
            "JD 里没拆出可比对的要求项（或全是软要求），岗位匹配度按中档记",
            {"decided": 0, "matched": 0, "unverified": unverified},
        )
    matched = sum(1 for gap in decided if str(gap.get("level")) == "matched")
    partial = sum(1 for gap in decided if str(gap.get("level")) == "partial")
    ratio = (matched + 0.5 * partial) / len(decided)
    score = _stepped(ratio, _FIT_STEPS)
    worst = next((gap for gap in decided if str(gap.get("level")) == "missing"), None)
    evidence = (
        f"{len(decided)} 条可比对要求里命中 {matched} 条、"
        f"勉强对上 {partial} 条，命中率 {round(ratio * 100)}%"
    )
    if worst is not None:
        evidence += f"；完全没证据的一条是「{str(worst.get('requirement'))[:30]}」"
    if unverified:
        evidence += f"（另有 {unverified} 条待核，不计入命中率）"
    return _dimension(
        "job_fit",
        score,
        evidence,
        {"decided": len(decided), "matched": matched, "partial": partial, "unverified": unverified},
    )


def _project_evidence(projects: Sequence[Mapping[str, Any]]) -> Dimension:
    count = len(projects)
    highlights = sum(
        len(item.get("highlights") or []) if isinstance(item.get("highlights"), Sequence) else 0
        for item in projects
    )
    if count >= 2 and highlights >= 3:
        score = 5
    elif count >= 2:
        score = 4
    elif count == 1 and highlights >= 1:
        score = 3
    elif count == 1:
        score = 2
    else:
        score = 1
    named = [str(item.get("name") or "").strip() for item in projects]
    sample = next((name for name in named if name), "")
    evidence = f"认出 {count} 段项目、{highlights} 条成果要点"
    evidence += f"，如「{sample[:30]}」" if sample else "，没有任何项目条目可深挖"
    return _dimension(
        "project_evidence", score, evidence, {"projects": count, "highlights": highlights}
    )


def _dimension(
    name: DimensionName, score: int, evidence: str, signals: dict[str, Any]
) -> Dimension:
    label = next(label for key, label in _DIMENSIONS if key == name)
    return Dimension(
        name=name, label=label, score=max(1, min(5, score)), evidence=evidence, signals=signals
    )


def _stepped(value: float, steps: Sequence[tuple[float, int]]) -> int:
    for floor, score in steps:
        if value >= floor:
            return score
    return 1


def _as_mappings(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def derive_focus_topics(
    parsed: Mapping[str, Any],
    gaps: Sequence[Mapping[str, Any]],
    dimensions: Sequence[Dimension],
    *,
    max_topics: int = MAX_FOCUS_TOPICS,
) -> list[dict[str, Any]]:
    """弱项优先的主题清单，`plan` 的槽位就从这里铺。

    三类来源，按"越该攻越在前"排：
    1. JD 缺口：`missing` 记 1 分、`partial` 记 2 分、`unverified` 记 3 分（没依据更要现场验）；
    2. 得分 <= `WEAK_SCORE` 的维度本身（技能广度塌了、项目实证塌了，这类问题不是某一项
       技能能盖住的，只能围绕该维度出题）；
    3. 候选人自称"精通"的技能——不是弱项，但必须验真，排在最后，配额有余才轮到。
    """
    topics: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(
        topic: str,
        dimension: str,
        score: int,
        why: str,
        evidence: str | None,
        source: str,
    ) -> None:
        text = collapse(topic)
        key = text.lower()
        if len(text) < 2 or key in seen:
            return
        seen.add(key)
        topics.append(
            {
                "topic": text,
                "dimension": dimension,
                "score": score,
                "why": why,
                "evidence": evidence,
                "source": source,
            }
        )

    for gap in gaps:
        level = str(gap.get("level"))
        requirement = str(gap.get("requirement") or "")
        skill = str(gap.get("skill") or "") or requirement
        if level == "missing":
            add(skill, "job_fit", 1, f"JD 要求「{requirement[:30]}」，简历里找不到证据", None, "jd")
        elif level == "partial":
            add(
                skill,
                "job_fit",
                2,
                f"JD 要 {gap.get('required_level')}，简历只写到 {gap.get('resume_level')}",
                str(gap.get("evidence") or "") or None,
                "jd",
            )
        elif level == "unverified":
            add(
                requirement,
                "job_fit",
                3,
                "这条要求词典和模型都没给出可核对的证据，面试现场问最直接",
                None,
                "jd",
            )

    covered = {str(topic["dimension"]) for topic in topics}
    for dimension in dimensions:
        if dimension.score > WEAK_SCORE or dimension.name in covered:
            continue
        add(
            dimension.label,
            dimension.name,
            dimension.score,
            dimension.evidence,
            None,
            "rubric",
        )

    for claimed in _as_mappings(parsed.get("skills")):
        if str(claimed.get("level") or "") != "proficient":
            continue
        add(
            str(claimed.get("name") or ""),
            "skill_depth",
            4,
            "简历自称精通，出题验真的收益最高",
            str(claimed.get("evidence") or "") or None,
            "resume",
        )

    # 稳定排序：同分时保持插入顺序（缺口在前、维度居中、自称精通在后）
    topics.sort(key=lambda item: int(item["score"]))
    return topics[:max_topics]


def confidence_from_signals(
    parsed: Mapping[str, Any], gaps: Sequence[Mapping[str, Any]]
) -> float:
    """规则版的画像置信度：五份依据各值 0.12，起步 0.4，上限 0.94。

    为什么不用模型自评的数：模型给置信度普遍偏高（写 0.9 不需要勇气），而这里每一项
    都对应"这条画像到底有几分材料撑着"，是可以复核的。模型的自评只作为参考值并
    取两者较小者（见 `profile_node`）。
    """
    score = 0.4
    if _as_mappings(parsed.get("skills")):
        score += 0.12
    if isinstance(parsed.get("years_experience"), (int, float)):
        score += 0.12
    if _as_mappings(parsed.get("projects")):
        score += 0.1
    if any(str(gap.get("level")) in ("matched", "partial", "missing") for gap in gaps):
        score += 0.1
    if len(gaps) >= 3:
        score += 0.08
    return round(min(score, 0.94), 2)


def fallback_narrative(
    parsed: Mapping[str, Any],
    dimensions: Sequence[Dimension],
    topics: Sequence[Mapping[str, Any]],
    confidence: float,
) -> dict[str, Any]:
    """模型没跑成时用的叙述，全部由规则拼出——画像不能因为一次调用失败就变成空壳。"""
    weakest = [dim for dim in dimensions if dim.score <= WEAK_SCORE]
    strongest = [dim for dim in dimensions if dim.score >= 4]
    name = str(parsed.get("name") or "该候选人")
    years = parsed.get("years_experience")
    head = f"{name}，工作 {years if years is not None else '未知'} 年"
    if topics:
        first = topics[0]
        head += f"；最该先问的是{first['topic']}（{first['why']}）"
    else:
        head += "；没有可用的主题，建议先补一份 JD 或技能清单"
    return {
        "summary": head + "。",
        "strengths": [f"{dim.label}：{dim.evidence}" for dim in strongest],
        "weaknesses": [f"{dim.label}：{dim.evidence}" for dim in weakest],
        "confidence": confidence,
        "narrative_source": "rubric",
    }
