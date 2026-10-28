"""validate 节点的接线测试：规则与 LLM 反思的分工、自增计数、图级有界回退（全程离线）。

这里锁的是**通道行为**：谁在什么条件下写 `validate_issues`、什么时候才允许花钱问模型、
回退预算耗尽后图还能不能走完。不断言模型文案，LLM 回复一律预置。
"""

import json
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.agents.nodes.validate_node import make_validate_node
from app.agents.state import GRAPH_NODES, AgentState, NodeName
from app.agents.workflow import (
    PROMPT_VERSION_KEY,
    build_agent_graph,
    make_thread_config,
    open_checkpointer,
)
from app.config import get_settings
from app.exceptions.base import LLMError
from app.utils.llm_client import LLMClient

from ._fake_llm import FakeCompletions, stamp

#: 两段都写死结束日期，年限就与"今天"无关，测试不会跨年变红
_CLEAN_TEXT = """姓名：陈昊 13800138000

技能
Java 精通

工作经历
甲公司 后端开发 2021.07-2023.06
    负责订单服务
乙公司 高级开发 2023.07-2024.06

教育经历
某大学 本科 软件工程 2017-2021
"""
_INVERTED_TEXT = """姓名：陈昊

工作经历
甲公司 后端开发 2023.05-2022.01
    负责订单服务
"""
_YEARS = 3.0


def _parsed(text: str = _CLEAN_TEXT) -> dict[str, Any]:
    return {
        "name": "陈昊",
        "phone": "13800138000",
        "years_experience": _YEARS,
        "skills": [{"name": "Java", "level": "proficient", "evidence": "Java 精通"}],
        "educations": [],
        "work_experiences": [],
        "projects": [],
    }


def _node(
    replies: Sequence[str] = ('{"issues": []}',), raises: Exception | None = None
) -> tuple[Any, FakeCompletions]:
    fake = FakeCompletions(replies, raises)
    settings = get_settings()
    return make_validate_node(llm=LLMClient(settings=settings, client=fake)), fake


async def test_a_clean_resume_lets_the_model_have_the_last_word() -> None:
    node, fake = _node(['{"issues": ["work_experiences 里的公司名疑似填成了岗位名"]}'])

    update = await node({"raw_text": _CLEAN_TEXT, "parsed": _parsed()})

    assert update["reflect_issues"] == ["work_experiences 里的公司名疑似填成了岗位名"]
    assert update["validate_issues"] == [], "反思不是规则问题，不进回退通道（ADR-0022）"
    assert "validate_retries" not in update, "反思不烧 parse 的重跑预算"
    assert update["errors"] == ["validate(reflect): work_experiences 里的公司名疑似填成了岗位名"]
    assert update["parsed"] == _parsed(), "没东西可修时原样覆盖写"
    assert update[PROMPT_VERSION_KEY] == stamp("validate")
    assert len(fake.calls) == 1


async def test_the_reflection_is_only_trusted_when_it_names_a_field() -> None:
    """parse 是确定性的：一条凭感觉的批评会把重试预算烧光也修不掉任何东西。"""
    node, _ = _node(['{"issues": ["整体读起来不像真人写的", "排版有点乱"]}'])

    update = await node({"raw_text": _CLEAN_TEXT, "parsed": _parsed()})

    assert update["validate_issues"] == []
    assert "reflect_issues" not in update, "闸门放行零条就不必开一个空通道"
    assert "validate_retries" not in update, "没判出问题就不许计数"


async def test_the_prompt_carries_todays_date_so_time_judgements_have_a_basis() -> None:
    """真调实测：不给日期，模型会拿自己的知识截止日当"今天"，把正常离职日期判成未来。"""
    node, fake = _node()

    await node({"raw_text": _CLEAN_TEXT, "parsed": _parsed()})

    rendered = fake.calls[0]["messages"][1]["content"]
    assert date.today().isoformat() in rendered


async def test_rules_that_already_failed_do_not_buy_a_model_call() -> None:
    node, fake = _node()

    update = await node({"raw_text": _INVERTED_TEXT, "parsed": _parsed(_INVERTED_TEXT)})

    assert any("倒挂" in issue for issue in update["validate_issues"])
    assert update["validate_retries"] == 1
    assert fake.calls == [], "规则还挂着时让 parse 重跑，别先花钱"
    assert PROMPT_VERSION_KEY not in update


async def test_repairs_land_in_the_append_only_errors_channel() -> None:
    parsed = _parsed()
    parsed["projects"] = [{"name": "刷单系统", "evidence": "原文里没有的一句话"}]
    node, _ = _node()

    update = await node({"raw_text": _CLEAN_TEXT, "parsed": parsed, "errors": ["旧值"]})

    assert len(update["errors"]) == 1, "只返回增量，别把历史冲掉"
    assert update["errors"][0].startswith("validate: ")
    assert update["parsed"]["projects"] == []


async def test_a_zero_padded_year_is_backfilled_instead_of_complained_about() -> None:
    parsed = _parsed()
    parsed["years_experience"] = 0.0
    node, _ = _node()

    update = await node({"raw_text": _CLEAN_TEXT, "parsed": parsed})

    assert update["parsed"]["years_experience"] == pytest.approx(_YEARS)
    assert any("补成" in note for note in update["errors"])


async def test_no_extraction_result_is_an_issue_not_a_crash() -> None:
    node, fake = _node()

    update = await node({"raw_text": _CLEAN_TEXT})

    assert update["validate_issues"] == ["还没有抽取结果，校验无从下手"]
    assert "parsed" not in update, "不许把 None 覆盖成空 dict"
    assert fake.calls == []


async def test_a_broken_reviewer_does_not_hijack_the_fallback_gate() -> None:
    """反思环节坏了不该拦住流程，更不该让 parse 白跑：规则已经点头了。"""
    node, _ = _node(raises=LLMError("上游挂了"))

    update = await node({"raw_text": _CLEAN_TEXT, "parsed": _parsed()})

    assert update["validate_issues"] == []
    assert "validate_retries" not in update
    assert PROMPT_VERSION_KEY not in update, "没用成的模板不该进 trace"


async def test_the_node_is_idempotent() -> None:
    node, _ = _node()
    state: AgentState = {"raw_text": _INVERTED_TEXT, "parsed": _parsed(_INVERTED_TEXT)}

    first = await node(state)
    second = await node(state)

    first.pop("validate_retries")
    second.pop("validate_retries")
    assert first == second


_REAL_REFLECTION = (
    Path(__file__).parent.parent / "fixtures" / "llm" / "validate_liang_qingsheng.json"
)
#: 测试里把字段名写死，不从节点导入——否则这条断言就变成自我循环论证
_FIELDS = (
    "name",
    "email",
    "phone",
    "years_experience",
    "skills",
    "educations",
    "work_experiences",
    "projects",
)


async def test_the_recorded_real_reflection_only_yields_actionable_issues() -> None:
    """回放 2026-10-16 对真简历的一次真调。

    只回放 `response` 字符串，所以这里断言的是**闸门的行为**：真模型的每一条意见都必须
    点得出 `ResumeSchema` 里的字段名才放行——不然一条凭感觉的批评就能把重试预算烧光。
    """
    if not _REAL_REFLECTION.is_file():
        pytest.skip("缺少录制件")
    recorded = json.loads(_REAL_REFLECTION.read_text(encoding="utf-8"))
    node, _ = _node([recorded["response"]])

    update = await node({"raw_text": _CLEAN_TEXT, "parsed": _parsed()})

    issues = update["reflect_issues"]
    assert issues, "这份真简历确实有可点名的问题"
    assert all(any(field in issue for field in _FIELDS) for issue in issues)
    assert update["validate_issues"] == [], "反思全归观测通道，一条都不进回退判据"
    assert "validate_retries" not in update, "一轮反思不再烧 parse 的重跑预算"
    # 不读录制件里的 prompt_version：那是"当时用了哪版模板"的历史事实，
    # 而这里断的是"节点盖的是当前配置那版"——部署参数换了，这条也该照常成立
    assert update[PROMPT_VERSION_KEY] == stamp("validate")


def _noop(name: NodeName) -> Any:
    async def node(state: AgentState) -> Mapping[str, Any]:
        return {}

    return node


async def test_a_dirty_resume_uses_up_the_budget_and_still_walks_to_the_end(
    tmp_path: Path,
) -> None:
    """计划书 M2 的验收：倒挂样本让 parse 恰好重跑 `validate_max_retries + 1` 次，
    预算耗尽后带着 `validate_issues` 继续往下走，而不是中断整张图。"""
    agent = get_settings().agent

    async def parse(state: AgentState) -> Mapping[str, Any]:
        return {"raw_text": _INVERTED_TEXT, "parsed": _parsed(_INVERTED_TEXT)}

    nodes = {name: _noop(name) for name in GRAPH_NODES}
    nodes[NodeName.PARSE] = parse
    nodes[NodeName.VALIDATE], fake = _node()

    async with open_checkpointer(tmp_path / "ckpt.db") as saver:
        graph = build_agent_graph(nodes, checkpointer=saver)
        result = await graph.ainvoke({}, make_thread_config("m2-1", recursion_limit=30))

    ran = [step["node"] for step in result["trace"]]
    assert ran.count(NodeName.PARSE.value) == agent.validate_max_retries + 1
    assert ran.count(NodeName.VALIDATE.value) == agent.validate_max_retries + 1
    assert fake.calls == [], "三次校验全卡在规则层，一次模型都没问"
    assert len(result["validate_issues"]) == 1, "规则是确定性的，同一条问题重复三轮"
    assert result["validate_retries"] == agent.validate_max_retries + 1
    assert [step["node"] for step in result["trace"]][-1] == NodeName.REVIEW.value
    assert NodeName.MATCH.value in ran, "预算耗尽也要放行下游"


async def test_reflection_issues_never_buy_a_second_parse_round(tmp_path: Path) -> None:
    """D-4(a) 的验收〔T-201 / ADR-0022〕：规则全过、反思挑出问题时图只走一趟 parse。

    分家之前这条会变成"parse 跑满 `validate_max_retries + 1` 次、每次 `parsed` 逐字相同"，
    实测 2026-10-16 的真简历就是这么白烧了三轮。
    """

    async def parse(state: AgentState) -> Mapping[str, Any]:
        return {"raw_text": _CLEAN_TEXT, "parsed": _parsed()}

    nodes = {name: _noop(name) for name in GRAPH_NODES}
    nodes[NodeName.PARSE] = parse
    nodes[NodeName.VALIDATE], _ = _node(['{"issues": ["work_experiences 疑似写成了岗位名"]}'])

    async with open_checkpointer(tmp_path / "ckpt.db") as saver:
        graph = build_agent_graph(nodes, checkpointer=saver)
        result = await graph.ainvoke({}, make_thread_config("m2-2", recursion_limit=30))

    ran = [step["node"] for step in result["trace"]]
    assert ran.count(NodeName.PARSE.value) == 1, "反思修不掉，重跑一次就是纯浪费"
    assert "validate_retries" not in result or result["validate_retries"] == 0
    assert result["reflect_issues"] == ["work_experiences 疑似写成了岗位名"]
    assert result["validate_issues"] == []
    assert [step["node"] for step in result["trace"]][-1] == NodeName.REVIEW.value
