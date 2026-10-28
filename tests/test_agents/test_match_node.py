"""match 节点的接线测试：词典判完就不花钱、模型判定要过证据闸、检索只走接口（全程离线）。

这里锁的是**通道行为**：什么条件下才允许调模型、模型的话哪些能进 `matched_gaps`、
调用失败时缺口清单还剩什么。断言一律落在结构与调用次数上，不落模型文案。

S2 起还锁两件事的形状：软要求**分批**（批数、每批条数、批失败时的隔离）与送进 prompt 的
**可引用叙述**（`<resume_evidence>` 的内容、引用预算）。后者钉的是模板段落，所以用例自己
把模板版本钉死，不跟着环境的部署参数漂。
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from app.agents.nodes.match_node import (
    _MAX_EVIDENCE_CHARS,
    _MAX_JUDGE_BATCHES,
    _MAX_UNJUDGED_PER_CALL,
    _resume_evidence,
    make_match_node,
)
from app.agents.workflow import PROMPT_VERSION_KEY
from app.config import get_settings
from app.exceptions.base import LLMError, LLMResponseError, ValidationFailedError
from app.extractors.base import evidence_belongs
from app.extractors.project_extractor import extract_projects
from app.extractors.skill_extractor import extract_skills
from app.extractors.timeline_extractor import extract_timeline
from app.parsers.pdf_parser import PdfParser
from app.prompts import registry
from app.prompts.registry import get_prompt
from app.services.match_service import split_requirements
from app.utils.llm_client import LLMClient
from app.vectorstore.retriever import RetrievedPassage

from ._fake_llm import FakeCompletions, completion, prompt_block, stamp

#: 真简历只用来验"可引用叙述过得了证据闸"这一条地基（见文末那条用例），不掺进别的用例
REAL_RESUME = Path(__file__).parent.parent / "fixtures" / "resumes" / "附件简历-梁清生.pdf"

#: 前两行点得到词典里的技术名，第三条是软要求——只有它会触发模型调用
_JD = """岗位职责：
1. 负责订单服务的开发与维护

任职要求：
1. 精通 Java
2. 熟悉 MySQL 索引原理
3. 具备较强的沟通能力

加分项：
1. 熟悉 RocketMQ
"""

_RAW = """姓名：陈昊

技能
Java 精通
熟悉 MySQL 索引原理

自我评价
具备较强的沟通能力
"""


def _parsed(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "陈昊",
        "years_experience": 3.0,
        "skills": [
            {"name": "Java", "level": "proficient", "evidence": "Java 精通"},
            {"name": "MySQL", "level": "familiar", "evidence": "熟悉 MySQL 索引原理"},
        ],
        "projects": [],
    }
    base.update(overrides)
    return base


def _node(
    replies: Sequence[str] = (),
    raises: Exception | None = None,
    retriever: Any | None = None,
    settings: Any | None = None,
    fake: FakeCompletions | None = None,
) -> tuple[Any, FakeCompletions]:
    fake = fake or FakeCompletions(replies or ('{"gaps": []}',), raises)
    node = make_match_node(
        llm=LLMClient(settings=settings or get_settings(), client=fake),
        retriever=retriever,
    )
    return node, fake


async def _run(node: Any, **overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {"jd_text": _JD, "raw_text": _RAW, "parsed": _parsed()}
    state.update(overrides)
    return await node(state)


def _gap_of(gaps: list[dict[str, Any]], skill: str) -> dict[str, Any]:
    return next(gap for gap in gaps if gap.get("skill") == skill)


class _RecordingRetriever:
    """假向量库：记下每次召回的 query 与 top_k，返回固定片段。"""

    def __init__(self, passages: Sequence[str] = ("Redis 缓存与持久化配置",)) -> None:
        self.passages = list(passages)
        self.queries: list[tuple[str, int]] = []

    async def retrieve(self, query: str, top_k: int) -> Sequence[RetrievedPassage]:
        self.queries.append((query, top_k))
        return [RetrievedPassage(text=text, score=0.9) for text in self.passages]


class _JudgeEveryRequirementInBatch(FakeCompletions):
    """按 prompt 里 `<jd_requirements>` 的实际条数造回复〔T-204 的验收替身〕。

    分批之后"预置一串回复"不再够用：每批要判的不是同几条要求。证据直接抄要求本身，
    所以配套的 `raw_text` 里必须带上这些句子——过的是真证据闸，不是绕过它。
    """

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        requirements = prompt_block(str(kwargs["messages"][-1]["content"]), "jd_requirements")
        gaps = [
            {
                "requirement": row["requirement"],
                "level": "matched",
                "evidence": row["requirement"],
            }
            for row in requirements
        ]
        return completion(json.dumps({"gaps": gaps}, ensure_ascii=False))


class _JudgeExcept(_JudgeEveryRequirementInBatch):
    """判定同上，但凡某批的清单里出现 `marker` 就整批抛错〔批间隔离的探针〕。

    按**内容**而不是按调用次序锁定失败的批：并发下发出的先后不等于批序，按次序锁会随
    机器负载随机红。
    """

    def __init__(self, marker: str) -> None:
        super().__init__()
        self.marker = marker

    async def create(self, **kwargs: Any) -> Any:
        requirements = prompt_block(str(kwargs["messages"][-1]["content"]), "jd_requirements")
        if any(self.marker in str(row["requirement"]) for row in requirements):
            self.calls.append(kwargs)
            raise LLMError("这一批坏了", code="llm_timeout")
        return await super().create(**kwargs)


def _soft_jd(count: int) -> tuple[str, str, list[str]]:
    """造一份只有软要求的 JD，并返回**逐条抄了这些要求**的简历原文与条目清单。

    JD 与简历必须同源于同一个列表：分批用例断的是"每条都被判过"，而判定能不能落地全看
    证据在不在原文里——两边各写一遍字符串，迟早对不上然后红得莫名其妙。
    """
    items = [f"第{i}项需要很强的跨团队协作意识体现" for i in range(1, count + 1)]
    jd = "任职要求：\n" + "；".join(items) + "\n"
    return jd, "自我评价\n" + "\n".join(items) + "\n", items


def _pin_template_version(version: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """把取模板的版本钉成 `version`，其余 agent 参数原样保留。

    只改这一个字段：整块换成 `AgentParams(prompt_version=...)` 会把 `validate_max_retries`
    一起打回代码默认值，用例红起来看不出是被谁影响的。
    版本必须由用例自己钉而不是跟着环境读：这几条断的是**模板形状**，环境哪天切回 v1，
    跟着配置走的那几条就变成假绿。
    """
    agent = get_settings().agent.model_copy(update={"prompt_version": version})
    chosen = get_settings().model_copy(update={"agent": agent})
    monkeypatch.setattr(registry, "get_settings", lambda app_env=None: chosen)


@pytest.fixture
def match_template_v1(monkeypatch: pytest.MonkeyPatch) -> None:
    """`<resume_evidence>` 是 v2 才有的段落，所以"v1 照旧渲染"得先把版本钉回 v1。"""
    _pin_template_version("v1", monkeypatch)


@pytest.fixture
def match_template_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    """钉在 `v2`〔T-203〕。"""
    _pin_template_version("v2", monkeypatch)


async def test_dictionary_decided_requirements_cost_no_llm_call() -> None:
    """JD 通篇点得出技术名时一次模型都不花——这是"确定性优先"的账单证明。"""
    jd = "任职要求：\n1. 精通 Java\n2. 熟悉 MySQL"
    node, fake = _node()

    update = await _run(node, jd_text=jd)

    assert fake.calls == []
    assert PROMPT_VERSION_KEY not in update, "没用到模板就别留版本号"
    assert {"Java", "MySQL"} == {gap["skill"] for gap in update["matched_gaps"]}


async def test_soft_requirement_is_the_only_thing_that_buys_a_call() -> None:
    node, fake = _node(
        ['{"gaps": [{"requirement": "具备较强的沟通能力", "level": "matched",'
          ' "evidence": "具备较强的沟通能力"}]}']
    )

    update = await _run(node)

    assert len(fake.calls) == 1, "词典判不上的那一类才值得花钱，且只花一次"
    assert update[PROMPT_VERSION_KEY] == stamp("match")


async def test_rules_side_gaps_survive_when_the_model_is_consulted() -> None:
    node, _ = _node(['{"gaps": []}'])

    gaps = (await _run(node))["matched_gaps"]

    assert _gap_of(gaps, "Java")["level"] == "matched"
    assert _gap_of(gaps, "RocketMQ")["level"] == "missing"
    assert _gap_of(gaps, "RocketMQ")["section"] == "preferred"


async def test_every_gap_row_has_the_same_shape() -> None:
    """下游按字段读，缺一个键就是一处 KeyError；来源不同的行必须同形。"""
    node, _ = _node(
        ['{"gaps": [{"requirement": "具备较强的沟通能力", "level": "matched",'
          ' "evidence": "具备较强的沟通能力"}]}']
    )

    gaps = (await _run(node))["matched_gaps"]

    keys = {
        "requirement",
        "level",
        "evidence",
        "skill",
        "required_level",
        "resume_level",
        "section",
        "source",
    }
    assert gaps
    assert all(set(gap) == keys for gap in gaps)


async def test_model_claim_without_a_quotable_evidence_becomes_unverified() -> None:
    """无证据即丢弃：档位照收，证据照删，但要求本身不能被抹掉。"""
    node, _ = _node(
        ['{"gaps": [{"requirement": "具备较强的沟通能力", "level": "matched",'
          ' "evidence": "沟通能力强到可以独立对接客户"}]}']
    )

    update = await _run(node)

    gap = next(g for g in update["matched_gaps"] if g["requirement"] == "具备较强的沟通能力")
    assert gap["level"] == "unverified"
    assert gap["evidence"] is None
    assert any("找不到" in note for note in update["errors"])


async def test_invented_requirement_is_dropped_and_the_real_one_stays_listed() -> None:
    """模型顺手补了条 JD 里没有的要求：丢弃它，同时把真那条按待核列出，别让清单撒谎。"""
    node, _ = _node(
        ['{"gaps": [{"requirement": "熟悉 Kubernetes 集群运维", "level": "missing"}]}']
    )

    update = await _run(node)

    assert "熟悉 Kubernetes 集群运维" not in {gap["requirement"] for gap in update["matched_gaps"]}
    unjudged = [g for g in update["matched_gaps"] if g["source"] == "unjudged"]
    assert [gap["requirement"] for gap in unjudged] == ["具备较强的沟通能力"]
    assert any("不在待判定清单里" in note for note in update["errors"])


@pytest.mark.parametrize(
    "raises",
    [
        LLMError("上游没响应", code="llm_timeout"),
        LLMResponseError("连续不合格", details={"attempts": 2}),
    ],
    ids=["llm_error", "bad_json"],
)
async def test_model_failure_degrades_instead_of_aborting(raises: Exception) -> None:
    """可恢复的那一类：规则层判出的缺口照常往下走，软要求以 unverified 留名。"""
    node, _ = _node(raises=raises)

    update = await _run(node)

    gaps = update["matched_gaps"]
    assert {gap["skill"] for gap in gaps} >= {"Java", "MySQL", "RocketMQ"}
    soft = next(g for g in gaps if g["requirement"] == "具备较强的沟通能力")
    assert soft["level"] == "unverified"
    assert PROMPT_VERSION_KEY not in update
    assert any("按待核处理" in note for note in update["errors"])


async def test_prompt_carries_the_unresolved_requirements_and_the_resume_skills() -> None:
    node, fake = _node(['{"gaps": []}'])

    await _run(node)

    assert prompt_block(fake.user_text, "jd_requirements") == [
        {"requirement": "具备较强的沟通能力", "section": "required", "skills": []}
    ]
    skills = prompt_block(fake.user_text, "resume_skills")
    assert {item["name"] for item in skills} == {"Java", "MySQL"}


async def test_retriever_seam_is_called_with_the_settings_top_k() -> None:
    """阶段 6 换实现时只换这个对象：接口收到了 query 与 `top_k_final`，就算接上了。"""
    retriever = _RecordingRetriever()
    node, fake = _node(['{"gaps": []}'], retriever=retriever)

    await _run(node)

    settings = get_settings()
    assert retriever.queries == [("具备较强的沟通能力", settings.retrieval.top_k_final)]
    passages = prompt_block(fake.user_text, "resume_passages")
    assert passages[0]["text"] == "Redis 缓存与持久化配置"
    assert passages[0]["requirement"] == "具备较强的沟通能力"


async def test_null_retriever_is_the_default_and_retrieves_nothing() -> None:
    node, fake = _node(['{"gaps": []}'])

    update = await _run(node)

    assert prompt_block(fake.user_text, "resume_passages") == []
    assert update["matched_gaps"]


async def test_soft_requirements_are_judged_batch_by_batch() -> None:
    """〔T-204 验收〕20 条软要求的 JD：每条都拿到 `source=llm` 判定，调用次数等于批数。

    分批之前是"一次 prompt 只装得下 12 条，剩下的永远不判"；分批以后账单确实变长了
    （两次而不是一次），换来的是缺口清单不再谎称"模型看过了"。
    """
    jd, raw, items = _soft_jd(_MAX_UNJUDGED_PER_CALL + 8)
    node, fake = _node(fake=_JudgeEveryRequirementInBatch())

    update = await _run(node, jd_text=jd, raw_text=raw)

    expected_calls = -(-len(items) // _MAX_UNJUDGED_PER_CALL)
    judged = [gap for gap in update["matched_gaps"] if gap["source"] == "llm"]
    assert len(fake.calls) == expected_calls, "一次调用一批，不多不少"
    assert all(
        len(prompt_block(call["messages"][-1]["content"], "jd_requirements"))
        <= _MAX_UNJUDGED_PER_CALL
        for call in fake.calls
    ), "每批的上限就是 prompt 里的条数"
    assert {gap["requirement"] for gap in judged} == set(items)
    assert not [gap for gap in update["matched_gaps"] if gap["source"] == "unjudged"]


async def test_the_number_of_batches_is_capped_and_the_tail_stays_named() -> None:
    """批数封顶〔T-204〕：一份把什么都写成要求的 JD 不该烧穿预算，但少判了要留一句为什么。"""
    tail = 12
    jd, raw, items = _soft_jd(_MAX_UNJUDGED_PER_CALL * _MAX_JUDGE_BATCHES + tail)
    node, fake = _node(fake=_JudgeEveryRequirementInBatch())

    update = await _run(node, jd_text=jd, raw_text=raw)

    gaps = update["matched_gaps"]
    assert len(fake.calls) == _MAX_JUDGE_BATCHES
    judged = {gap["requirement"] for gap in gaps if gap["source"] == "llm"}
    assert judged == set(items[: _MAX_UNJUDGED_PER_CALL * _MAX_JUDGE_BATCHES])
    assert {gap["requirement"] for gap in gaps if gap["source"] == "unjudged"} == set(
        items[len(judged) :]
    )
    assert any("按待核处理" in note for note in update["errors"])


async def test_one_broken_batch_does_not_take_the_others_down() -> None:
    """批与批互相隔离：坏掉那批的要求以待核留名，好那批的判定照常进清单。"""
    jd, raw, items = _soft_jd(_MAX_UNJUDGED_PER_CALL + 5)
    # 第二条批从 `items[12]` 开始，所以拿它当"这一批该失败"的标记
    node, fake = _node(fake=_JudgeExcept(items[_MAX_UNJUDGED_PER_CALL]))

    update = await _run(node, jd_text=jd, raw_text=raw)

    gaps = update["matched_gaps"]
    assert {gap["requirement"] for gap in gaps if gap["source"] == "llm"} == set(
        items[:_MAX_UNJUDGED_PER_CALL]
    )
    assert {gap["requirement"] for gap in gaps if gap["source"] == "unjudged"} == set(
        items[_MAX_UNJUDGED_PER_CALL:]
    )
    assert any("按待核处理" in note for note in update["errors"])


async def test_the_prompt_carries_the_quotable_resume_narratives(
    match_template_v2: None,
) -> None:
    """〔T-203〕工作履历 `summary` 与项目 `highlights` 进 prompt，模型才有得抄。

    断的是围栏里的**结构**：`kind` / `ref` / `text` 是模板里给模型的引用锚点，少一个键
    模型就说不清这段话出自哪一段。
    """
    parsed = _parsed(
        work_experiences=[{"company": "某物流", "summary": "负责订单履约链路的容量规划"}],
        projects=[{"name": "库存中心", "highlights": ["库存周转率提升 18%"]}],
    )
    node, fake = _node(['{"gaps": []}'])

    await _run(node, parsed=parsed)

    assert prompt_block(fake.user_text, "resume_evidence") == [
        {"kind": "work", "ref": "某物流", "text": "负责订单履约链路的容量规划"},
        {"kind": "project", "ref": "库存中心", "text": "库存周转率提升 18%"},
    ]


def test_v1_still_renders_without_the_evidence_block(match_template_v1: None) -> None:
    """切版本期间 v1 必须还能跑：节点多传的那个变量不该让旧模板当场炸。"""
    messages = get_prompt("match").render(
        requirements_json="[]",
        skills_json="[]",
        passages_json="[]",
        evidence_json='[{"text": "不该出现"}]',
    )

    assert "不该出现" not in messages[-1]["content"]
    assert prompt_block(messages[-1]["content"], "resume_passages") == []


async def test_the_evidence_block_stops_at_its_char_budget_and_says_so(
    match_template_v2: None,
) -> None:
    """〔T-203〕叙述段每批都要重发，所以有引用预算；超出的部分**留一句说明**而不是静默丢。"""
    long_summary = "负责" + "容量规划与稳定性治理" * 20
    many = [{"company": f"公司{i}", "summary": long_summary} for i in range(30)]
    node, fake = _node(['{"gaps": []}'])

    update = await _run(node, parsed=_parsed(work_experiences=many))

    sent = prompt_block(fake.user_text, "resume_evidence")
    assert len(sent) < len(many), "一条都没挡下来就说明这条用例没在测预算"
    assert sum(len(str(row["text"])) for row in sent) <= _MAX_EVIDENCE_CHARS
    assert any("未送进模型" in note for note in update["errors"])


async def test_an_oversized_narrative_is_cut_at_the_budget_not_dropped_whole(
    match_template_v2: None,
) -> None:
    """单段就超预算时留下前缀：`work_experiences[*].summary` 的长度没有上限（只做换行连接），
    整段丢掉会让模型一份可抄的叙述都看不到。前缀仍是原文子串，过得了证据闸。"""
    huge = "负责" + "容量规划与稳定性治理" * 500
    work = [{"company": "某物流", "summary": huge}]
    node, fake = _node(['{"gaps": []}'])

    update = await _run(node, parsed=_parsed(work_experiences=work))

    sent = prompt_block(fake.user_text, "resume_evidence")
    assert len(sent) == 1
    assert len(str(sent[0]["text"])) == _MAX_EVIDENCE_CHARS
    assert huge.startswith(str(sent[0]["text"]))
    assert any("引用预算边界" in note for note in update["errors"])


def test_every_quotable_row_on_the_real_resume_survives_the_evidence_gate() -> None:
    """〔T-203 的地基〕真简历抽出的每一段叙述都必须逐字过得了证据闸。

    这条不锁住，v2 模板就是负收益：模型照围栏里的话抄，抄一句被判废一句，`matched`
    反而比 v1 更少。走真 `PdfParser` 与三个确定性抽取器，全程离线、不调模型。
    """
    if not REAL_RESUME.is_file():
        pytest.skip("缺少真简历样本")
    raw_text = PdfParser().parse(REAL_RESUME).raw_text
    fields: dict[str, Any] = {}
    fields.update(extract_timeline(raw_text).as_resume_fields())
    fields.update(extract_skills(raw_text).as_resume_fields())
    fields.update(extract_projects(raw_text).as_resume_fields())

    items, note = _resume_evidence(fields)

    assert items, "真简历一段可引用叙述都抽不出来，说明上游变了形"
    assert not note, "实测这份只有 1054 字，撞不到引用预算"
    assert all(evidence_belongs(raw_text, str(row["text"])) for row in items)


async def test_missing_parsed_or_jd_raises_instead_of_returning_an_empty_list() -> None:
    node, fake = _node()

    with pytest.raises(ValidationFailedError) as caught:
        await node({"jd_text": _JD, "raw_text": _RAW})
    assert caught.value.code == "match_no_parsed"

    with pytest.raises(ValidationFailedError) as caught:
        await _run(node, jd_text="   ")
    assert caught.value.code == "match_no_jd"
    assert fake.calls == []


async def test_rerunning_the_node_on_fallback_yields_the_same_gaps() -> None:
    """幂等：回退会让本节点重跑，清单不能因为跑过一遍就变。"""
    node, _ = _node(['{"gaps": []}'])

    first = await _run(node)
    second = await _run(node)

    assert first["matched_gaps"] == second["matched_gaps"]


async def test_update_only_touches_declared_channels() -> None:
    node, _ = _node(['{"gaps": []}'])

    update = await _run(node)

    assert set(update) <= {"matched_gaps", "errors", PROMPT_VERSION_KEY}
    assert isinstance(update["errors"], list), "追加通道只给增量，不给全表"


async def test_a_resume_without_skills_turns_every_named_requirement_missing() -> None:
    node, _ = _node()

    gaps = (await _run(node, parsed=_parsed(skills=[])))["matched_gaps"]

    assert {gap["level"] for gap in gaps if gap["skill"]} == {"missing"}


async def test_duties_are_not_gaps() -> None:
    node, _ = _node(['{"gaps": []}'])

    update = await _run(node)

    requirements = split_requirements(_JD)
    duty_texts = {item.text for item in requirements if item.section == "duty"}
    assert duty_texts, "拆不到职责就说明 JD 样例失效了"
    assert not (duty_texts & {gap["requirement"] for gap in update["matched_gaps"]})
    assert any("岗位职责" in note for note in update["errors"])
