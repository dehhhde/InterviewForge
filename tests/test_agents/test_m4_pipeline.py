"""M4 图级验收：match → profile → plan 三段真节点在编好的图里跑通（全程离线）。

单节点测试管的是各自的闸门，这里管**接缝**：缺口清单进得了画像、画像的主题进得了配额，
并且计划书 M4 那条验收（`plan.items` 的 count 之和 == `settings.question.total`）
在真图上成立。parse/validate/generate/review 用空节点占位——它们各自的测试在别处。
"""

import json
from typing import Any

from app.agents.nodes.match_node import make_match_node
from app.agents.nodes.plan_node import make_plan_node
from app.agents.nodes.profile_node import make_profile_node
from app.agents.state import GRAPH_NODES, PIPELINE, NodeName
from app.agents.workflow import build_agent_graph, make_thread_config, open_checkpointer
from app.config import get_settings
from app.utils.llm_client import LLMClient

from ._fake_llm import FakeCompletions

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

_PARSED: dict[str, Any] = {
    "name": "陈昊",
    "years_experience": 3.0,
    "skills": [
        {"name": "Java", "level": "proficient", "evidence": "Java 精通"},
        {"name": "MySQL", "level": "familiar", "evidence": "熟悉 MySQL 索引原理"},
    ],
    "projects": [{"name": "订单中心", "highlights": ["日峰 2k 单"]}],
}

#: 判定内容本身由 `test_match_node.py` 覆盖；这里要它采纳成功，好让 trace 里出现版本号
_NO_GAPS = json.dumps(
    {
        "gaps": [
            {
                "requirement": "具备较强的沟通能力",
                "level": "matched",
                "evidence": "具备较强的沟通能力",
            }
        ]
    },
    ensure_ascii=False,
)
_NO_NARRATIVE = json.dumps(
    {
        "summary": "Java 扎实，Redis 空白。",
        "strengths": [],
        "weaknesses": [],
        "confidence": 0.6,
    },
    ensure_ascii=False,
)


async def _noop(state: Any) -> dict[str, Any]:
    """占位节点：其余四个节点各有自己的测试，这里只验三段真节点之间的接缝。"""
    return {}


def _nodes(**overrides: Any) -> dict[NodeName, Any]:
    settings = get_settings()
    base = {name: _noop for name in GRAPH_NODES}
    base.update(
        match=make_match_node(llm=LLMClient(settings=settings, client=FakeCompletions([_NO_GAPS]))),
        profile=make_profile_node(
            llm=LLMClient(settings=settings, client=FakeCompletions([_NO_NARRATIVE]))
        ),
        plan=make_plan_node(settings=settings),
    )
    base.update(overrides)
    return base


async def _invoke(thread: str, tmp_path: Any) -> dict[str, Any]:
    async with open_checkpointer(tmp_path / f"ckpt-{thread}.db") as saver:
        graph = build_agent_graph(_nodes(), checkpointer=saver)
        return await graph.ainvoke(
            {"resume_id": "r1", "jd_text": _JD, "raw_text": _RAW, "parsed": _PARSED},
            make_thread_config(thread, recursion_limit=30),
        )


async def test_the_chain_lands_the_plan_on_the_configured_total(tmp_path: Any) -> None:
    """M4 验收第一条：所有 `count` 加起来正好是配置的总题数。"""
    result = await _invoke("m4-total", tmp_path)

    plan = result["plan"]
    settings = get_settings()
    assert sum(int(item["count"]) for item in plan["items"]) == settings.question.total
    assert plan["total"] == settings.question.total


async def test_difficulty_distribution_stays_within_one_question(tmp_path: Any) -> None:
    """M4 验收第二条：难度分布与 `difficulty_ratio` 一致，误差 ≤ 1 题。"""
    result = await _invoke("m4-ratio", tmp_path)

    settings = get_settings()
    totals: dict[str, int] = {}
    for item in result["plan"]["items"]:
        totals[str(item["difficulty"])] = totals.get(str(item["difficulty"]), 0) + int(
            item["count"]
        )
    for name, share in settings.question.difficulty_ratio.model_dump().items():
        assert abs(totals.get(name, 0) - share * settings.question.total) <= 1


async def test_gaps_flow_into_the_profile_and_the_plan(tmp_path: Any) -> None:
    """接缝：词典判出的缺口要一路走到配额表，中间没有一环把它咽掉。"""
    result = await _invoke("m4-flow", tmp_path)

    gaps = {str(gap["skill"]) for gap in result["matched_gaps"] if gap.get("skill")}
    assert {"Java", "MySQL", "RocketMQ"} <= gaps
    assert {item["name"] for item in result["profile"]["dimensions"]}
    topics = {str(item["topic"]) for item in result["profile"]["focus_topics"]}
    assert "RocketMQ" in topics, "JD 缺口没进主题清单"
    assert {item["topic"] for item in result["plan"]["items"]} <= topics
    assert any(
        gap["source"] == "llm" and gap["level"] == "matched" for gap in result["matched_gaps"]
    ), "软要求的模型判定没接住"


async def test_the_same_state_twice_gives_the_byte_identical_plan(tmp_path: Any) -> None:
    """确定性要到图级为止：同一份输入跑两遍，`plan` 一个字节都不许差。"""
    first = await _invoke("m4-det-1", tmp_path)
    second = await _invoke("m4-det-2", tmp_path)

    dump = json.dumps(first["plan"], ensure_ascii=False, sort_keys=True)
    assert dump == json.dumps(second["plan"], ensure_ascii=False, sort_keys=True)


async def test_prompt_versions_of_the_two_llm_nodes_land_in_trace(tmp_path: Any) -> None:
    """模板版本号只进 trace：它是归因用的元数据，不是 state 通道。"""
    result = await _invoke("m4-trace", tmp_path)

    steps = {step["node"]: step for step in result["trace"]}
    assert [step["node"] for step in result["trace"]] == [name.value for name in PIPELINE]
    assert steps[NodeName.MATCH.value]["prompt_version"] == "match@v1"
    assert steps[NodeName.PROFILE.value]["prompt_version"] == "profile@v1"
    assert "prompt_version" not in steps[NodeName.PLAN.value], "plan 不调模型，没有版本号可记"
