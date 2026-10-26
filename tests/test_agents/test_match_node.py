"""match 节点的接线测试：词典判完就不花钱、模型判定要过证据闸、检索只走接口（全程离线）。

这里锁的是**通道行为**：什么条件下才允许调模型、模型的话哪些能进 `matched_gaps`、
调用失败时缺口清单还剩什么。断言一律落在结构与调用次数上，不落模型文案。
"""

from collections.abc import Sequence
from typing import Any

import pytest

from app.agents.nodes.match_node import make_match_node
from app.agents.workflow import PROMPT_VERSION_KEY
from app.config import get_settings
from app.exceptions.base import LLMError, LLMResponseError, ValidationFailedError
from app.services.match_service import split_requirements
from app.utils.llm_client import LLMClient
from app.vectorstore.retriever import RetrievedPassage

from ._fake_llm import FakeCompletions, prompt_block

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
) -> tuple[Any, FakeCompletions]:
    fake = FakeCompletions(replies or ('{"gaps": []}',), raises)
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
    assert update[PROMPT_VERSION_KEY] == "match@v1"


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

    assert prompt_block(fake.user_text, "待判定要求") == [
        {"requirement": "具备较强的沟通能力", "section": "required", "skills": []}
    ]
    skills = prompt_block(fake.user_text, "简历已抽出的技能")
    assert {item["name"] for item in skills} == {"Java", "MySQL"}


async def test_retriever_seam_is_called_with_the_settings_top_k() -> None:
    """阶段 6 换实现时只换这个对象：接口收到了 query 与 `top_k_final`，就算接上了。"""
    retriever = _RecordingRetriever()
    node, fake = _node(['{"gaps": []}'], retriever=retriever)

    await _run(node)

    settings = get_settings()
    assert retriever.queries == [("具备较强的沟通能力", settings.retrieval.top_k_final)]
    passages = prompt_block(fake.user_text, "向量检索到的简历相近片段")
    assert passages[0]["text"] == "Redis 缓存与持久化配置"
    assert passages[0]["requirement"] == "具备较强的沟通能力"


async def test_null_retriever_is_the_default_and_retrieves_nothing() -> None:
    node, fake = _node(['{"gaps": []}'])

    update = await _run(node)

    assert prompt_block(fake.user_text, "向量检索到的简历相近片段") == []
    assert update["matched_gaps"]


async def test_only_a_bounded_batch_goes_to_the_model() -> None:
    """软要求堆起来时不能无限塞 prompt：超出的按待核处理，且留一句为什么少判了。"""
    soft = "；".join(f"第{i}项需要很强的跨团队协作意识体现" for i in range(1, 20))
    jd = f"任职要求：\n{soft}"
    node, fake = _node(['{"gaps": []}'])

    update = await _run(node, jd_text=jd)

    assert len(fake.calls) == 1
    assert len(prompt_block(fake.user_text, "待判定要求")) == 12
    assert len([g for g in update["matched_gaps"] if g["source"] == "unjudged"]) == 19
    assert any("本轮只送" in note for note in update["errors"])


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
