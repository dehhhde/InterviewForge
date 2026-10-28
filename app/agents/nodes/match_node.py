"""岗位匹配节点：先规则对齐，剩下的才问模型〔阶段 7 · §10.4.3〕

节点契约（计划书 §4）在这里的落法：
- 只返回增量：`matched_gaps` + 可选的 `errors`，`prompt_version` 走保留键交给 `traced()`；
- 依赖全部装配期注入（`llm` / `retriever` / `settings`），节点内部不 new client；
- **检索分支只有接口**：`retriever` 默认是 `NullRetriever`，恒召回 0 条，所以 M4 跑的是
  无检索降级版；阶段 6 换成 sqlite-vec 实现时本文件一行都不用改；
- 幂等：`build_match` 是纯函数，回退重跑只会算出同一份缺口清单。

为什么词典判完还要留一次模型调用：JD 里总有一批不带技术名的软要求
（"较强的沟通能力"、"能快速熟悉业务"），正则对它们无解，而这些恰恰是面试要问的。
模型只判**这些**，且它给出的证据必须能在简历原文里逐字找到，否则降级成 `unverified`。

S2 之后这里多了两件事（都是为了让"钱花了结论别丢"）：
- **软要求分批**（T-204）：一批最多 `_MAX_UNJUDGED_PER_CALL` 条，超出 `_MAX_JUDGE_BATCHES`
  批的部分仍按待核留名——分批解决"永远不判"，上限解决"一份啰嗦 JD 烧穿预算"。
- **给模型可引用的叙述**（T-203）：`work_experiences[*].summary` 与 `projects[*].highlights`
  进 prompt（模板 `match@v2` 起才有这一段；v1 不引用它，所以传了也不影响渲染）。
"""

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agents.state import AgentState
from app.agents.workflow import PROMPT_VERSION_KEY, Node
from app.config import Settings, get_settings
from app.exceptions.base import LLMError, LLMResponseError, ValidationFailedError
from app.prompts.registry import get_prompt
from app.services.match_service import (
    Requirement,
    adopt_llm_gaps,
    build_match,
    make_gap,
)
from app.utils.llm_client import LLMClient
from app.vectorstore.retriever import NullRetriever, RetrievedPassage, SkillRetriever

#: 一次 prompt 最多带几条待判定要求。JD 写得越啰嗦软要求越多，不设上限就是拿 token
#: 换重复劳动，而且模型在后面几条上的注意力会明显下降
_MAX_UNJUDGED_PER_CALL = 12

#: 分批的上限〔T-204〕。4 批 = 48 条软要求，已经远超真 JD 的量级（实测样本个位数），
#: 再往上就是"一份把职责当要求写的 JD"在烧钱；被这条挡下的部分照旧以 `unjudged` 留名。
#: 与 `_MAX_UNJUDGED_PER_CALL` 同族，所以放代码常量而不是 yaml：它定义的是本节点的
#: 送问形状，不是部署差异（同 `profile_service` 的 rubric 常量）。
_MAX_JUDGE_BATCHES = 4

#: 送进 prompt 的简历叙述总字数〔T-203〕。工作/项目段落长度不可预测，而这段每批都要重发一遍，
#: 所以按文档顺序累加到这条就停（停下的事实会记进 `errors`，不是静默截断）。
_MAX_EVIDENCE_CHARS = 4000


class _GapJudgement(BaseModel):
    requirement: str = Field(min_length=2)
    level: Literal["matched", "partial", "missing"]
    evidence: str | None = None


class _MatchPayload(BaseModel):
    """`match.system.j2` 约定的返回形状。"""

    gaps: list[_GapJudgement] = Field(default_factory=list)


def make_match_node(
    *,
    llm: LLMClient | None = None,
    retriever: SkillRetriever | None = None,
    settings: Settings | None = None,
) -> Node:
    """造 match 节点。词典能判的就一次模型都不花。"""
    settings = settings or (llm.settings if llm is not None else get_settings())
    client = llm or LLMClient(settings)
    retrieval = retriever or NullRetriever()

    async def match_node(state: AgentState) -> dict[str, Any]:
        parsed = state.get("parsed")
        if not isinstance(parsed, dict):
            # 没有抽取结果时无从比对。这不是"质量问题"而是"没输入"：回退预算在 validate
            # 那一头已经花完，此处返回空清单只会让画像替一份不存在的简历编证据
            raise ValidationFailedError(
                "缺少简历抽取结果，无法与岗位要求比对",
                code="match_no_parsed",
                details={"resume_id": state.get("resume_id")},
            )
        jd_text = (state.get("jd_text") or "").strip()
        if not jd_text:
            raise ValidationFailedError(
                "缺少岗位 JD 原文，无法比对",
                code="match_no_jd",
                details={"resume_id": state.get("resume_id")},
            )

        raw_text = state.get("raw_text") or ""
        result = build_match(jd_text, parsed)
        errors = [f"match: {note}" for note in result.notes]
        gaps = list(result.gaps)
        stamp = ""

        if result.unresolved:
            adopted, stamp, llm_errors = await _judge(
                client, retrieval, result.unresolved, parsed, raw_text, settings
            )
            gaps += adopted
            errors += llm_errors
            # 模型没接住的（含整次调用失败）也要留名：它们以 unverified 进清单，
            # 下游把它们当"没依据"，而不是当"JD 没这条要求"
            adopted_texts = {str(gap["requirement"]) for gap in adopted}
            gaps += [
                make_gap(item.text, "unverified", section=item.section, source="unjudged")
                for item in result.unresolved
                if item.text not in adopted_texts
            ]

        update: dict[str, Any] = {"matched_gaps": gaps}
        if errors:
            update["errors"] = errors
        if stamp:
            update[PROMPT_VERSION_KEY] = stamp
        return update

    return match_node


@dataclass(frozen=True, slots=True)
class _PromptInputs:
    """除"这一批待判定要求"之外，每批都要重发的那三份材料。

    收成一个对象是为了让 `_judge_one` 的参数表只有一个批次相关的东西：批与批之间不同的
    只有要求清单，三份输入是同一份，散着传容易在某一路上漏掉一个（漏 `jd_text` 那次
    就是同族错误，AGENTS.md §4 第 8 条）。
    """

    skills_json: str
    evidence_json: str
    raw_text: str


async def _judge(
    client: LLMClient,
    retrieval: SkillRetriever,
    unresolved: Sequence[Requirement],
    parsed: Mapping[str, Any],
    raw_text: str,
    settings: Settings,
) -> tuple[list[dict[str, Any]], str, list[str]]:
    """给词典判不了的要求补模型判定：分批并发，一批一次调用。返回（可用缺口，模板标记，说明）。"""
    evidence, budget_note = _resume_evidence(parsed)
    inputs = _PromptInputs(
        skills_json=_dump(parsed.get("skills") or []),
        evidence_json=_dump(evidence),
        raw_text=raw_text,
    )
    batches = [
        list(unresolved[start : start + _MAX_UNJUDGED_PER_CALL])
        for start in range(0, len(unresolved), _MAX_UNJUDGED_PER_CALL)
    ][:_MAX_JUDGE_BATCHES]

    notes: list[str] = []
    if budget_note:
        notes.append(f"match: {budget_note}")
    sent = sum(len(batch) for batch in batches)
    if len(unresolved) > sent:
        notes.append(
            f"待判定要求共 {len(unresolved)} 条，送满 {_MAX_JUDGE_BATCHES} 批只容得下 {sent} 条，"
            "其余按待核处理"
        )

    # 批与批互不依赖，所以并发；`gather` 的结果按提交顺序回来，缺口清单的次序因此是确定的
    # （幂等要求：回退重跑本节点必须产出同一份清单，AGENTS.md §4 第 6 条）。
    # 真正的限流点仍然是 `LLMClient` 里那把信号量，这里不再叠一层。
    outcomes = await asyncio.gather(
        *(_judge_one(client, retrieval, batch, inputs, settings) for batch in batches)
    )
    adopted: list[dict[str, Any]] = []
    stamp = ""
    for batch_adopted, batch_stamp, batch_notes in outcomes:
        adopted += batch_adopted
        notes += batch_notes
        stamp = stamp or batch_stamp
    return adopted, stamp, notes


async def _judge_one(
    client: LLMClient,
    retrieval: SkillRetriever,
    batch: Sequence[Requirement],
    inputs: _PromptInputs,
    settings: Settings,
) -> tuple[list[dict[str, Any]], str, list[str]]:
    """一批软要求的一次往返。整批失败也只折这一批：其余批与规则层的缺口照常往下走。"""
    passages = await _retrieve(retrieval, batch, settings)
    prompt = get_prompt("match")
    try:
        payload = await client.chat_json(
            "extract",
            prompt.render(
                requirements_json=_dump([item.as_dict() for item in batch]),
                skills_json=inputs.skills_json,
                passages_json=_dump(passages),
                evidence_json=inputs.evidence_json,
            ),
            schema=_MatchPayload,
            temperature=0.0,
        )
    except (LLMError, LLMResponseError) as exc:
        # 模型坏了不拦路：规则层已经判出的缺口照常往下走
        return [], "", [f"match: 软要求的模型判定没跑成（{exc.code}），这些要求按待核处理"]
    adopted, dropped = adopt_llm_gaps(
        (item.model_dump() for item in payload.gaps),
        requirements=batch,
        raw_text=inputs.raw_text,
    )
    notes = [f"match: {note}" for note in dropped]
    # 一条都没采纳成功就别留模板标记：trace 里的版本号是给"哪版模板变差了"归因的，
    # 用上的那次才算数
    stamp = f"{prompt.node}@{prompt.version}" if adopted else ""
    return adopted, stamp, notes


def _resume_evidence(parsed: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """挑出"模型能逐字抄"的简历叙述。返回（片段清单，超预算说明；不超就空串）。

    只取 `work_experiences[*].summary` 与 `projects[*].highlights`〔T-203 / BACKLOG P2
    「match 的模型分支看不到它要引用的文本」〕。选这两处的理由是**它们过得了证据闸**：
    都是确定性抽取的产物，`join_wrapped_lines()`（T-102）保证抹掉空白后仍是原文子串，
    模型从这里抄的句子拿去和 `raw_text` 逐字核对就能对上。
    不塞整份 `raw_text`：那里栏目标题、日期串都是"原文里有、但不是证据"的东西，
    模型抄上去只会让判定整批降成 unverified——钱花了，结论还是丢了。

    预算是**硬上限**：整段放不下就截到边界（截出来的是逐字前缀，仍然过得了证据闸），
    因为工作履历的 `summary` 长度没有上限（`timeline_extractor` 只做换行连接）。
    超出的段落**留说明**而不是静默丢掉：静默截断等于骗模型"简历就写了这么多"。
    """
    items: list[dict[str, Any]] = []
    used = 0
    skipped = 0
    clipped = 0
    for entry in _narratives(parsed):
        text = str(entry["text"])
        remaining = _MAX_EVIDENCE_CHARS - used
        if remaining <= 0:
            skipped += 1
            continue
        if len(text) > remaining:
            entry = {**entry, "text": text[:remaining]}
            clipped += 1
        items.append(entry)
        used += len(str(entry["text"]))
    notes: list[str] = []
    if clipped:
        notes.append(f"{clipped} 段叙述被截到 {_MAX_EVIDENCE_CHARS} 字的引用预算边界")
    if skipped:
        notes.append(f"{skipped} 段工作/项目叙述超出引用预算，未送进模型")
    return items, "；".join(notes)


def _narratives(parsed: Mapping[str, Any]) -> Sequence[dict[str, Any]]:
    """按文档顺序产出可引用片段：先工作履历，再项目成果。

    `ref` 带着公司名/项目名，是为了让模型判"带队经验"这类要求时说得出这段话出自哪一段；
    它同时是人读 prompt 时的定位符，不参与任何核对。
    """
    out: list[dict[str, Any]] = []
    for work in _as_rows(parsed.get("work_experiences")):
        summary = str(work.get("summary") or "").strip()
        if summary:
            out.append({"kind": "work", "ref": str(work.get("company") or ""), "text": summary})
    for project in _as_rows(parsed.get("projects")):
        for highlight in _as_rows(project.get("highlights"), as_str=True):
            text = str(highlight).strip()
            if text:
                out.append({"kind": "project", "ref": str(project.get("name") or ""), "text": text})
    return out


def _as_rows(value: Any, *, as_str: bool = False) -> list[Any]:
    """把"可能是列表、可能是脏数据"的 JSON 片段收敛成行。

    `as_str=True` 时只认字符串元素（`highlights` 里不该有映射，出现就说明上游变了形）；
    默认只认映射。两种形态都在这里收口，调用点不必各自 `isinstance` 一遍。
    """
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return []
    if as_str:
        return [item for item in value if isinstance(item, str)]
    return [item for item in value if isinstance(item, Mapping)]


async def _retrieve(
    retrieval: SkillRetriever, batch: Sequence[Requirement], settings: Settings
) -> list[dict[str, Any]]:
    """检索分支的现调用点。`NullRetriever` 下恒为空列表，一次向量库都不碰。"""
    out: list[dict[str, Any]] = []
    for item in batch:
        found: Sequence[RetrievedPassage] = await retrieval.retrieve(
            item.text, settings.retrieval.top_k_final
        )
        out.extend(
            {
                "requirement": item.text,
                "text": passage.text,
                "score": round(float(passage.score), 4),
            }
            for passage in found
        )
    return out


def _dump(payload: Any) -> str:
    """`ensure_ascii=False`：中文以中文出现，省 token 也好读。"""
    return json.dumps(payload, ensure_ascii=False, default=str)
