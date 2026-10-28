"""`generate` 的两个节点：扇出准备（零 LLM）与出题分支（一次调用一槽位）〔M5〕

断言全在结构与调用行为上：批次号、`Overwrite` 哨兵、模型槽位、题号偏移、失败时少出题
而不是抛异常。文案一条不断（AGENTS.md §6）。
"""

import json
from typing import Any

import pytest
from langgraph.types import Overwrite

from app.agents.nodes.generate_node import (
    _messages,
    make_generate_item_node,
    make_generate_node,
)
from app.agents.workflow import PROMPT_VERSION_KEY, GenerateJob, route_after_generate
from app.config import Settings
from app.exceptions.base import BudgetExceededError, LLMError, ValidationFailedError
from app.prompts.registry import get_prompt
from app.utils.llm_client import LLMClient
from tests.test_agents._fake_llm import FakeCompletions, prompt_block, slot_settings, stamp

EVIDENCE = "使用 Redis 实现滑动窗口限流"
RAW = f"项目经历\n{EVIDENCE}，QPS 从 800 提升到 2000。"


def _settings() -> Settings:
    """三个模型槽位三个可区分的名字，模型路由才有断言的余地。"""
    return slot_settings(extract="extract-slot", generate="generate-slot", review="review-slot")


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
    assert update[PROMPT_VERSION_KEY] == stamp("generate")
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
    assert prompt_block(user, "resume_parsed")["name"] == "林可"
    assert "并发" in user and "hard" in user and "请出 2 道题" in user
    assert prompt_block(user, "roster") == [
        {"topic": "存储", "difficulty": "medium", "count": 1}
    ]


async def test_a_broken_call_loses_one_slot_not_the_whole_run() -> None:
    """少出题交给 `review` 按数量差额判，分支抛异常会让整条图崩在一次网络抖动上。"""
    fake = FakeCompletions(raises=LLMError("上游不通"))
    node = make_generate_item_node(llm=LLMClient(settings=_settings(), client=fake))

    update = await node(_job())

    assert "questions" not in update
    assert "本槽位 0 题" in update["errors"][0]


# --- 定向重做轮〔T-404 / ADR-0021〕：节点挑槽位，载荷带上上一版 ---


def _rows(*topics: str, batch: int = 1) -> list[dict[str, Any]]:
    """按 `order_index` 0..n 造一批题：槽位归属全靠号位区间，题干写什么不重要。"""
    return [
        {
            "stem": f"关于{topic}的第 {index} 道题",
            "difficulty": "medium",
            "source": "resume",
            "evidence": EVIDENCE,
            "topic": topic,
            "batch": batch,
            "order_index": index,
        }
        for index, topic in enumerate(topics)
    ]


async def test_the_fanout_node_replaces_only_the_named_slot() -> None:
    """授权过的第二轮只扇被点名的档，其余题**换批次原样带走**〔ADR-0021〕。

    `carried` 是这张卷子的另一半：`list_for_interview` 读最新批次，少带一档就交出一张缺页的卷。
    """
    node = make_generate_node()
    plan = _plan(2, 2)
    rows = _rows("甲", "甲", "乙", "乙")

    update = await node(
        {
            "plan": plan,
            "question_batch": 1,
            "auto_fix": True,
            "questions": rows,
            "review_issues": [{"index": 2, "problem": "题干信息不全", "fix": "补上数据量级"}],
        }
    )

    assert update["question_batch"] == 2
    slots = update["fanout_slots"]
    assert [slot["plan_item"]["topic"] for slot in slots] == ["主题2"], "只有 2-3 号位那一档被点名"
    assert slots[0]["order_start"] == 2, "重做轮占回原号位，配额表一个字没改"
    previous = slots[0]["previous"]
    assert [item["stem"] for item in previous] == ["关于乙的第 2 道题", "关于乙的第 3 道题"]
    assert previous[0]["issues"] == ["题干信息不全｜修法：补上数据量级"]
    assert previous[1]["issues"] == []

    overwrite = update["questions"]
    assert isinstance(overwrite, Overwrite)
    assert [(row["order_index"], row["batch"]) for row in overwrite.value] == [(0, 2), (1, 2)]
    assert "定向重做 1 条槽位" in update["errors"][0]
    assert "原样带走 2 题" in update["errors"][0]


async def test_a_second_batch_without_authorization_redoes_nothing() -> None:
    """没授权就没有重做：批次 2 也照整卷扇出、清空累积通道——一次成卷是默认形状。

    这条与上一条只差一个 `auto_fix` 键。判据不能藏在批次号里（"第二批就是重做"），
    否则同线程重放与定向重做会共用一个来路，而前者要的正是整卷重出。
    """
    node = make_generate_node()

    update = await node(
        {
            "plan": _plan(2, 2),
            "question_batch": 1,
            "questions": _rows("甲", "甲", "乙", "乙"),
            "review_issues": [{"index": 2, "problem": "题干信息不全"}],
        }
    )

    assert [slot["plan_item"]["topic"] for slot in update["fanout_slots"]] == ["主题1", "主题2"]
    assert isinstance(update["questions"], Overwrite)
    assert update["questions"].value == []
    assert "previous" not in update["fanout_slots"][0]


async def test_the_first_round_declares_no_previous_for_any_slot() -> None:
    """第一轮没有"上一版"可带：`fanout_slots` 里不出现 `previous` 键，载荷里则是空列表。

    两者形状不同是有意的——通道里"这一轮不是重做"由键缺省表达，而分支读的是 `job.get(...)`，
    那里缺键与"没有旧题"长得一模一样（AGENTS.md §3 的 T-205 教训），所以打包时必须补空。
    """
    update = await make_generate_node()({"plan": _plan(1, 1), "question_batch": 0})

    assert all("previous" not in slot for slot in update["fanout_slots"])
    sends = route_after_generate({**_job_state(), **update})
    assert [dict(send.arg)["previous"] for send in sends] == [[], []]


def _job_state() -> dict[str, Any]:
    return {
        "resume_id": "e" * 32,
        "raw_text": RAW,
        "jd_text": "需要熟悉并发与存储",
        "parsed": {"name": "林可"},
        "plan": _plan(1, 1),
    }


def test_only_the_new_template_renders_the_per_question_angles() -> None:
    """角度进 prompt 是 v2 的内容，v1 一字未改〔T-202〕——版本化模板的意义就在这儿。

    这里**显式点版本**而不是跟配置走：断的是"哪版模板消费哪个变量"这个永久事实，
    不是"当前环境用哪版"（那条由 `stamp()` 负责，AGENTS.md §7）。
    断的是我们自己的数据（角度值）有没有落到 prompt 里，不是模型文案。
    """
    angles = ["方案取舍", "规模化瓶颈"]
    job = _job(
        plan_item={
            "topic": "并发",
            "difficulty": "hard",
            "count": 2,
            "source": "resume",
            "angles": angles,
        }
    )

    rendered = {
        version: _messages(get_prompt("generate", version=version), job, count=2)[1]["content"]
        for version in ("v1", "v2")
    }

    assert all(angle not in rendered["v1"] for angle in angles)
    assert all(angle in rendered["v2"] for angle in angles)


def _fence_lines(rendered: str, tag: str) -> int:
    """`<tag>` 开栏出现几行。数行而不是看子串：模板开头的注入声明会把标签名写在散文里讲一遍。"""
    return sum(1 for line in rendered.splitlines() if line.strip() == f"<{tag}>")


def test_only_the_redo_template_renders_the_previous_version() -> None:
    """`<previous>` 是 v3 才有的段落〔T-404〕，v1/v2 一字未改——回炉那一次才有反馈可读。

    显式点版本，理由与上一条相同：断的是"哪版模板消费哪个变量"这个永久事实。
    断的是我们自己构造的旧题与批注落没落进 prompt，不是模型文案。
    """
    previous = [{"stem": "讲讲你的限流方案", "difficulty": "hard", "issues": ["题干信息不全"]}]
    redo_job = _job(previous=previous)
    first_job = _job()

    rendered = {
        version: _messages(get_prompt("generate", version=version), redo_job, count=2)[1]["content"]
        for version in ("v2", "v3")
    }

    assert "讲讲你的限流方案" not in rendered["v2"]
    assert _fence_lines(rendered["v2"], "previous") == 0
    assert prompt_block(rendered["v3"], "previous") == previous
    # 第一轮（载荷里 `previous` 为空）不渲染那段围栏：给模型看一个空段落只会引出一句道歉
    first = _messages(get_prompt("generate", version="v3"), first_job, count=2)[1]["content"]
    assert _fence_lines(first, "previous") == 0


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
