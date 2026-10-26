"""`generate` 的两个节点：扇出准备（零 LLM）与出题分支（一次调用一槽位）〔M5〕

断言全在结构与调用行为上：批次号、`Overwrite` 哨兵、模型槽位、题号偏移、失败时少出题
而不是抛异常。文案一条不断（AGENTS.md §6）。
"""

import json
from typing import Any

import pytest
from langgraph.types import Overwrite

from app.agents.nodes.generate_node import make_generate_item_node, make_generate_node
from app.agents.workflow import PROMPT_VERSION_KEY, GenerateJob
from app.config import Settings, get_settings
from app.exceptions.base import BudgetExceededError, LLMError, ValidationFailedError
from app.utils.llm_client import LLMClient
from tests.test_agents._fake_llm import FakeCompletions, prompt_block

EVIDENCE = "使用 Redis 实现滑动窗口限流"
RAW = f"项目经历\n{EVIDENCE}，QPS 从 800 提升到 2000。"


def _settings() -> Settings:
    """深拷贝并给三个模型槽位起三个可区分的名字，模型路由才有断言的余地。"""
    copy = get_settings().model_copy(deep=True)
    copy.llm_model_extract = "extract-slot"
    copy.llm_model_generate = "generate-slot"
    copy.llm_model_review = "review-slot"
    return copy


def _plan(*counts: int) -> dict[str, Any]:
    return {
        "items": [
            {"topic": f"主题{i}", "difficulty": "medium", "count": count, "source": "resume"}
            for i, count in enumerate(counts, start=1)
        ],
        "total": sum(counts),
    }


def _draft(stem: str, *, evidence: str = EVIDENCE, source: str = "resume") -> dict[str, Any]:
    return {
        "stem": stem,
        "difficulty": "medium",
        "source": source,
        "evidence": evidence,
        "checkpoints": ["考察限流取舍"],
        "reference_answer": "讲清算法与边界",
        "rubric": ["能说出两种算法的差别"],
        "follow_ups": ["热点 key 怎么办"],
    }


def _reply(*drafts: dict[str, Any]) -> str:
    return json.dumps({"questions": list(drafts)}, ensure_ascii=False)


def _ok(*stems: str) -> str:
    """一批"全部合格"的产出，省去每个用例都手写字段。"""
    return _reply(*[_draft(stem) for stem in stems])


def _job(**overrides: Any) -> GenerateJob:
    base: GenerateJob = {
        "plan_item": {"topic": "并发", "difficulty": "hard", "count": 2, "source": "resume"},
        "order_start": 3,
        "question_batch": 2,
        "raw_text": RAW,
        "roster": [{"topic": "存储", "difficulty": "medium", "count": 1}],
        "parsed": {"name": "林可", "skills": [{"name": "Redis", "evidence": EVIDENCE}]},
    }
    base.update(overrides)
    return base


# --- 扇出准备节点：一次 LLM 都不花 ---


async def test_the_fanout_node_spends_no_tokens_and_bumps_the_batch() -> None:
    node = make_generate_node()

    update = await node({"plan": _plan(2, 3), "question_batch": 0})

    assert update["question_batch"] == 1
    assert update[PROMPT_VERSION_KEY] == "generate@v1"
    assert "批次 1" in update["errors"][0]


async def test_the_accumulated_channel_is_replaced_not_appended_to() -> None:
    """风险 5 的核心：`questions` 是累积通道，扇出节点必须自己整批换掉。
    ADR-0015 之后一轮成卷，但同一线程被重放时不换就会叠成 2N 题。"""
    node = make_generate_node()

    update = await node({"plan": _plan(2), "question_batch": 1})
    overwrite = update["questions"]

    assert isinstance(overwrite, Overwrite)
    assert overwrite.value == []
    assert "批次 2" in update["errors"][0]


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"plan": {}},
        {"plan": {"items": []}},
        {"plan": {"items": [{"topic": "t", "count": 0}]}},
    ],
)
async def test_no_quota_table_is_an_unrecoverable_failure(state: dict[str, Any]) -> None:
    """没有配额表就没有扇出依据：抛异常比"静默产出 0 题"诚实——ADR-0015 之后 0 题也会照常
    落成一份 `completed` 的空报告，再也没有闸门替它兜底。"""
    with pytest.raises(ValidationFailedError, match="没有出题配额"):
        await make_generate_node()(state)


# --- 成本护栏（M6）：越线在扇出之前断，出题的钱一分不花 ---


def _guarded(limit: int, *, prompt: int, completion: int) -> tuple[LLMClient, Settings]:
    settings = _settings()
    settings.agent.max_tokens_per_run = limit
    client = LLMClient(settings=settings, client=FakeCompletions([]))
    client.usage.record("generate-slot", prompt, completion)
    return client, settings


async def test_cost_guard_interrupts_before_the_fanout_spends() -> None:
    client, settings = _guarded(100, prompt=90, completion=20)
    node = make_generate_node(llm=client, settings=settings)

    with pytest.raises(BudgetExceededError) as caught:
        await node({"plan": _plan(2)})

    assert caught.value.details["used_tokens"] == 110
    assert caught.value.details["limit_tokens"] == 100
    # 护栏只读用量：一次补全都没发出
    assert client.usage.calls == 1  # 只有 _guarded 里记账的那一笔


async def test_cost_guard_trips_at_exactly_the_limit() -> None:
    """边界取 `>=`：等于上限也算越线，因为下一批扇出必然超。"""
    client, settings = _guarded(100, prompt=60, completion=40)
    with pytest.raises(BudgetExceededError):
        await make_generate_node(llm=client, settings=settings)({"plan": _plan(1)})


async def test_cost_guard_lets_healthy_runs_fan_out() -> None:
    client, settings = _guarded(10_000, prompt=60, completion=30)
    update = await make_generate_node(llm=client, settings=settings)({"plan": _plan(1)})
    assert update["question_batch"] == 1


async def test_generate_without_llm_is_a_guardless_fanout() -> None:
    """不注入 `llm` 就没有护栏（图骨架测试的默认形态），而不是"预算无限"的伪装。"""
    settings = _settings()
    settings.agent.max_tokens_per_run = 1
    update = await make_generate_node(settings=settings)({"plan": _plan(1)})
    assert update["question_batch"] == 1


# --- 出题分支：一条槽位一次调用 ---


async def test_the_branch_uses_the_generate_slot_and_a_declared_budget() -> None:
    fake = FakeCompletions([_ok("讲讲你的限流方案是怎么落地的", "滑动窗口的过期 key 怎么清")])
    node = make_generate_item_node(llm=LLMClient(settings=_settings(), client=fake))

    await node(_job())

    assert len(fake.calls) == 1
    assert fake.calls[0]["model"] == "generate-slot", "出题走 generate 槽位，不与抽取混用"
    assert fake.calls[0]["max_tokens"] == _settings().llm.default_max_tokens
    assert fake.calls[0]["temperature"] == pytest.approx(0.3)


async def test_the_branch_carries_batch_and_order_start_from_its_payload() -> None:
    """`Send` 的载荷就是分支的全部输入：批次与题号只能从那里来，不能指望 state。"""
    fake = FakeCompletions([_ok("讲讲你的限流方案是怎么落地的", "再问一道足够长的题干")])
    node = make_generate_item_node(llm=LLMClient(settings=_settings(), client=fake))

    update = await node(_job())

    assert [q["order_index"] for q in update["questions"]] == [3, 4]
    assert [q["batch"] for q in update["questions"]] == [2, 2]
    assert [q["difficulty"] for q in update["questions"]] == ["hard", "hard"]


async def test_the_plan_item_is_what_gets_rendered_into_the_prompt() -> None:
    fake = FakeCompletions([_ok("讲讲你的限流方案是怎么落地的")])
    node = make_generate_item_node(llm=LLMClient(settings=_settings(), client=fake))

    await node(_job(order_start=0))

    user = fake.user_text
    assert prompt_block(user, "简历抽取结果")["name"] == "林可"
    assert "并发" in user and "hard" in user and "请出 2 道题" in user
    assert prompt_block(user, "同批其它槽位") == [
        {"topic": "存储", "difficulty": "medium", "count": 1}
    ]


async def test_a_broken_call_loses_one_slot_not_the_whole_run() -> None:
    """少出题交给 `review` 按数量差额判，分支抛异常会让整条图崩在一次网络抖动上。"""
    fake = FakeCompletions(raises=LLMError("上游不通"))
    node = make_generate_item_node(llm=LLMClient(settings=_settings(), client=fake))

    update = await node(_job())

    assert "questions" not in update
    assert "本槽位 0 题" in update["errors"][0]


async def test_the_branch_carries_the_job_ad_into_its_prompt() -> None:
    """`source=jd` 的槽位要引用 JD 原句，而分支只看得到 `Send` 载荷：JD 必须一起打包过去。"""
    jd = "岗位职责：负责高并发服务的容量规划与限流方案评审。"
    quote = "负责高并发服务的容量规划"
    fake = FakeCompletions(
        [_reply(_draft("讲讲你做过的限流方案评审", source="jd", evidence=quote))]
    )
    node = make_generate_item_node(llm=LLMClient(settings=_settings(), client=fake))

    update = await node(
        _job(
            jd_text=jd,
            plan_item={"topic": "容量规划", "difficulty": "hard", "count": 1, "source": "jd"},
        )
    )

    assert jd in fake.user_text
    assert len(update["questions"]) == 1
    assert update["questions"][0]["evidence"] == quote


async def test_a_jd_slot_without_the_job_ad_loses_its_questions() -> None:
    """载荷里没带 JD 时无据可核，整题丢掉——不能让分支退回"猜简历"这条路上。"""
    fake = FakeCompletions([_reply(_draft("讲讲你做过的限流方案评审", source="jd"))])
    node = make_generate_item_node(llm=LLMClient(settings=_settings(), client=fake))

    update = await node(
        _job(plan_item={"topic": "容量规划", "difficulty": "hard", "count": 1, "source": "jd"})
    )

    assert "questions" not in update or update["questions"] == []
    assert any("岗位 JD 原文里找不到" in note for note in update["errors"])


async def test_hallucinated_evidence_never_leaves_the_branch() -> None:
    reply = _reply(
        _draft("讲讲你的限流方案是怎么落地的"),
        _draft("你如何用 Kafka 做异地多活的高可用切换", evidence="我做过三地五中心的容灾"),
    )
    fake = FakeCompletions([reply])
    node = make_generate_item_node(llm=LLMClient(settings=_settings(), client=fake))

    update = await node(_job())

    assert len(update["questions"]) == 1
    assert update["questions"][0]["stem"] == "讲讲你的限流方案是怎么落地的"
    assert any("丢弃一题" in note for note in update["errors"])
