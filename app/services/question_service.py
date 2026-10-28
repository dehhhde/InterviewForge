"""出题的规则层：草稿收口、引用核对、批次工序号〔阶段 7 · §10.4.6〕

本文件是 `generate` 的**闸门层**：不碰 LLM、不碰数据库、零成本、可单测。分工是——

- 模型只负责"这道题问什么、怎么答"；
- **难度不由模型定**：槽位的难度来自 `plan.items`（M4 的最大余数法配额），模型自报的难度
  一律被盖章覆盖。理由同 ADR-0009——配额表要满足"每档与 ratio×total 误差 ≤ 1"，
  而模型会把 hard 题写成 medium 的措辞、把 medium 题标成 hard，配额就废了；
- **引用必须逐字出自它声称的那份原文**：`source=resume` 对简历 `raw_text`、`source=jd`
  对岗位 `jd_text`（`evidence_belongs`，抹掉空白与零宽字符后比对）。核不上就**整题丢弃**
  并记进 notes——这是抽取层"无证据即丢弃"铁律在出题侧的翻版。留着一道凭空声称"你在项目里
  用过 X"的题，比少一道题糟糕得多：面试官照着问，候选人答不上来，锅在项目身上；
  为什么按来源分档而不是一刀切只认简历，见 `verify_evidence`；
- 工序号（`order_index`）在扇出**之前**就算好，不靠分支完成的先后：并行分支的返回顺序
  取决于调度，报告里的题号却是给人看的，必须每次一样。

`notes` 的去处：这是给人看的流水账，节点把它并进只追加的 `errors` 通道；
`review` 那头的 `review_issues` 只点名哪几题有问题，不再驱动回退（ADR-0015）。
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.extractors.base import collapse, evidence_belongs
from app.schemas.question import QuestionSchema

#: 单条 `plan.items` 槽位一次最多要几道题。配额本身由 `plan` 算出来（通常 1–3 题），
#: 这个上限只防"外部塞进一张手写的配额表"时把一次调用变成写小作文
MAX_QUESTIONS_PER_ITEM = 6

#: 模型给的 `source` 只认这三个；表外的值当 `generic`（保守：不声称基于简历）
_KNOWN_SOURCES: dict[str, str] = {"resume": "resume", "jd": "jd", "generic": "generic"}

#: 每种来源的引用该去哪儿核，以及给人看的说法。`generic` 不在表里：它不声称出处，引用可选
SOURCE_CORPUS_LABELS: dict[str, str] = {"resume": "简历原文", "jd": "岗位 JD 原文"}

#: 语料键名 -> 它在 state / `GenerateJob` 里的字段名。`corpora_for` 发的就是这些键。
_CORPUS_FIELDS = ("raw_text", "jd_text")


def corpora_for(source: str) -> tuple[str, ...]:
    """这道题的引用要到哪几份语料里逐字核对。**扇出瘦身与闸门共用这一个判据**。

    `workflow.route_after_generate` 靠它决定"这个槽位该带哪份原文"，`verify_evidence` 靠它
    决定"拿哪份原文去核"。分成两处写就会漂移：载荷少带一份语料，闸门在那一档里查不到，
    题被整批丢弃——和 2026-10-20 漏 `jd_text` 那次是同一个形状的事故。
    """
    if source not in SOURCE_CORPUS_LABELS:
        # `generic` 与任何表外来源：它不声称出处，引用出自哪一侧都算
        return _CORPUS_FIELDS
    return ("raw_text",) if source == "resume" else ("jd_text",)


@dataclass(frozen=True, slots=True)
class _Drop:
    """"整题丢弃"的信号，带原因。

    用类型而不是字符串区分，是因为 `verify_evidence` 的返回值有两种后果——丢题，或者
    保留题目并记一句说明（比如 `generic` 的脏引用只置空）。都塞进一个字符串就得靠约定
    判断，调用方一旦写反就把好题扔了。
    """

    reason: str

#: 落库时 `question` 表真正有的列。`topic` / `dimension` 是 state 侧的观测字段，
#: 报告里靠 `interview.plan` 复原，不进题表
ROW_COLUMNS: tuple[str, ...] = (
    "stem",
    "difficulty",
    "source",
    "evidence",
    "checkpoints",
    "reference_answer",
    "rubric",
    "follow_ups",
)


class _QuestionDraft(BaseModel):
    """模型对单条槽位的作答形状。

    刻意写得比 `QuestionSchema` 松：`difficulty` / `source` 是字符串而不是 Literal，
    因为这两档由规则层覆盖或收口，让模型在松形状上失败 → 由我们记账，
    而不是让 `chat_json` 拿一句"输入不是合法的枚举值"去反复重问。
    """

    stem: str = ""
    difficulty: str = ""
    source: str = ""
    evidence: str = ""
    checkpoints: list[str] = Field(default_factory=list)
    reference_answer: str = ""
    rubric: list[str] = Field(default_factory=list)
    follow_ups: list[str] = Field(default_factory=list)


class QuestionPayload(BaseModel):
    """`generate.system.j2` 约定的返回形状（顶层必须是对象，`chat_json` 不吃裸数组）。"""

    questions: list[_QuestionDraft] = Field(default_factory=list)


def verify_evidence(
    source: str, evidence: str, *, raw_text: str, jd_text: str = ""
) -> tuple[str, _Drop | str | None]:
    """核对一句引用的出处。返回（可存的引用，丢弃信号 / 一句说明 / None）。

    按来源分档核对，不是只认简历原文。实测（2026-10-20 首次端到端验收）：`plan.items`
    里 26 个槽位有 21 个是 `source=jd`，而 prompt 只告诉模型"引用要抄简历"，于是这批题
    的 `evidence` 全空，带引用的比例被压到 25%，怎么重试都过不了 90% 的验收线。JD 全文
    本来就在 state 里（`match` 缺它直接抛异常），没有理由不让它当出处。

    - `resume` / `jd`：没有引用、或引用在对应原文里逐字找不到 → 丢弃整题。这两档的
      题干由头就是那句原文，留着就是幻觉题。对应原文本身为空时同样丢弃：无据可核的
      "基于 JD 出题"和编造没法区分，宁可少一题。
    - `generic`：引用可选。带了就在简历或 JD 里核对（它可能确实顺手引了原文），核不上
      只置空并记一句——题干没有声称出处，丢了它题就不能用了。

    两份语料的取舍一律走 `corpora_for`，别在这里再写一遍 `if source == ...`：扇出载荷
    带哪份原文用的是同一个判据，两处各写一遍就是两套闸门。
    """
    fields = {"raw_text": raw_text, "jd_text": jd_text}
    corpora = [fields[key] for key in corpora_for(source) if fields[key]]
    label = SOURCE_CORPUS_LABELS.get(source)

    if label is None:
        if not evidence:
            return "", None
        if any(evidence_belongs(corpus, evidence) for corpus in corpora):
            return evidence, None
        return "", f"非简历来源的题目带了核对不上的引用，已置空：{evidence[:40]!r}"

    if not evidence:
        return "", _Drop(f"声称基于{label}出题却没给原文引用")
    if not any(evidence_belongs(corpus, evidence) for corpus in corpora):
        return "", _Drop(f"引用在{label}里找不到：{evidence[:40]!r}")
    return evidence, None


def evidence_problem(
    source: str, evidence: str, *, raw_text: str, jd_text: str = ""
) -> str | None:
    """这道题的引用有没有"该丢"的硬伤；有就返回原因，没有返回 None。

    `review` 的规则层用它而不是自己写 `evidence_belongs`：出题闸门和评审闸门必须是同一个
    判据，否则会出现"generate 按 JD 放行、review 按简历判废"的两套标准（实测 2026-10-20
    的第一版就是这样，靠重试永远修不好）。
    """
    _, outcome = verify_evidence(source, evidence, raw_text=raw_text, jd_text=jd_text)
    return outcome.reason if isinstance(outcome, _Drop) else None


def adopt_questions(
    payload: QuestionPayload,
    *,
    item: Mapping[str, Any],
    batch: int,
    order_start: int,
    raw_text: str,
    jd_text: str = "",
) -> tuple[list[dict[str, Any]], list[str]]:
    """把一次模型产出收成可用的题目。返回（题目列表，给人看的说明）。

    丢弃的题只记 `notes`，不抛异常：一条槽位出 2/3 题不是故障，故障在 `review` 那里
    按"整批距 `plan.total` 差几题"统一判定（见 `review_service.rule_issues`）。
    """
    notes: list[str] = []
    difficulty = str(item.get("difficulty") or "medium")
    topic = str(item.get("topic") or "")
    adopted: list[dict[str, Any]] = []

    for draft in payload.questions[:MAX_QUESTIONS_PER_ITEM]:
        question, reason = _one_question(
            draft,
            difficulty=difficulty,
            topic=topic,
            raw_text=raw_text,
            jd_text=jd_text,
            notes=notes,
        )
        if question is None:
            notes.append(f"主题「{topic}」丢弃一题：{reason}")
            continue
        # 工序号按"采纳成功"的顺序发，不按模型给的顺序：丢掉的题不该在报告里留下空洞
        adopted.append({**question, "batch": batch, "order_index": order_start + len(adopted)})

    planned = int(item.get("count") or 0)
    if len(adopted) < planned:
        notes.append(f"主题「{topic}」计划 {planned} 题，实收 {len(adopted)} 题")
    return adopted, notes


def _one_question(
    draft: _QuestionDraft,
    *,
    difficulty: str,
    topic: str,
    raw_text: str,
    jd_text: str,
    notes: list[str],
) -> tuple[dict[str, Any] | None, str]:
    """单题闸门。返回（合格题目或 None，丢弃原因）。"""
    source = _KNOWN_SOURCES.get(draft.source.strip().lower(), "generic")
    evidence = collapse(draft.evidence)

    verified, outcome = verify_evidence(source, evidence, raw_text=raw_text, jd_text=jd_text)
    if isinstance(outcome, _Drop):
        return None, outcome.reason
    if outcome is not None:
        notes.append(outcome)
    evidence = verified

    if draft.difficulty and collapse(draft.difficulty) != difficulty:
        notes.append(f"模型把「{topic}」标成 {draft.difficulty!r}，按配额改回 {difficulty}")

    candidate: dict[str, Any] = {
        "stem": collapse(draft.stem),
        "difficulty": difficulty,
        "source": source,
        "evidence": evidence,
        "checkpoints": _clean_list(draft.checkpoints),
        "reference_answer": collapse(draft.reference_answer),
        "rubric": _clean_list(draft.rubric),
        "follow_ups": _clean_list(draft.follow_ups),
        "topic": topic,
    }
    try:
        validated = QuestionSchema.model_validate(candidate)
    except (ValidationError, ValueError) as exc:
        return None, f"结构不合格：{_brief(exc)}"
    return _from_schema(validated, topic=topic), ""


def _from_schema(question: QuestionSchema, *, topic: str) -> dict[str, Any]:
    """`QuestionSchema` → state 里的题目 dict（多带 `topic`，落库前由 `to_row` 摘掉）。"""
    return {
        "stem": question.stem,
        "difficulty": question.difficulty,
        "source": question.source,
        "evidence": question.evidence,
        "checkpoints": list(question.checkpoints),
        "reference_answer": question.reference_answer,
        "rubric": list(question.rubric),
        "follow_ups": list(question.follow_ups),
        "topic": topic,
    }


def to_row(question: Mapping[str, Any]) -> dict[str, Any]:
    """state 题目 dict → `QuestionRepository.add_many` 的一行。"""
    return {column: question.get(column) for column in ROW_COLUMNS}


def sort_key(question: Mapping[str, Any]) -> tuple[int, int]:
    """报告里的题号顺序：先批次再工序号。并行分支的到达顺序不参与决策。"""
    return (int(question.get("batch") or 0), int(question.get("order_index") or 0))


def order_questions(questions: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """按 `sort_key` 排定，返回新列表。`review` 的 `index` 与落库的 `order_index` 都以它为准。"""
    ordered = [dict(item) for item in questions if isinstance(item, Mapping)]
    return sorted(ordered, key=sort_key)


def planned_count(items: Sequence[Mapping[str, Any]]) -> int:
    """`plan.items` 一共计划出多少题。`review` 用它判断"这批题够不够数"。"""
    return sum(int(item.get("count") or 0) for item in items)


def evidence_ratio(questions: Sequence[Mapping[str, Any]]) -> float:
    """带可回溯引用的题目占比（`evidence` 非空即算）。

    这是计划书 M5 那条验收的口径（≥ 0.9），也是 `scripts/eval_e2e.py`（M6）要报的指标之一。
    分母是整批题，不是只算某一来源：报告里"每道题有多少能指回原文"才是面试官关心的数。
    分子不看来源是因为能留到这里的引用都已经被 `verify_evidence` 按各自原文核过一遍，
    `resume` 单独当分子会把 jd 题一律记成无依据（实测 2026-10-20：真简历跑出来 25%，
    而题目本身句句有出处）。
    """
    if not questions:
        return 0.0
    hits = sum(
        1
        for item in questions
        if isinstance(item, Mapping) and str(item.get("evidence") or "").strip()
    )
    return hits / len(questions)


def dump(payload: Any) -> str:
    """进 prompt 的 JSON。`ensure_ascii=False`：中文以中文出现，省 token 也好读。"""
    return json.dumps(payload, ensure_ascii=False, default=str)


def _clean_list(values: Sequence[str]) -> list[str]:
    """折叠空白、丢掉空条目、按出现顺序去重。重复的考察点对评分没有额外信息。"""
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = collapse(str(value))
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _brief(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(map(str, error['loc']))}: {error['msg']}" for error in exc.errors()[:2]
        )
    return str(exc)[:160]
