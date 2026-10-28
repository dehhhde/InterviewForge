"""LangGraph 基础设施与 LLM 客户端的冒烟测试（阶段 0-4 的验收替身节点）。"""

from collections.abc import Mapping
from typing import Any

import httpx
import pytest
from langgraph.types import Overwrite
from openai import APIConnectionError
from pydantic import BaseModel

from app.agents.state import GRAPH_NODES, PIPELINE, AgentState, NodeName
from app.agents.workflow import (
    build_agent_graph,
    failed_trace_step,
    make_thread_config,
    open_checkpointer,
    read_thread_state,
)
from app.config import get_settings
from app.exceptions.base import AppError, LLMError, LLMResponseError
from app.utils.llm_client import LLMClient
from tests.test_agents._fake_llm import FakeCompletions


def _noop(name: NodeName) -> Any:
    async def node(state: AgentState) -> Mapping[str, Any]:
        return {}

    return node


def _nodes(**overrides: Any) -> dict[NodeName, Any]:
    base = {name: _noop(name) for name in GRAPH_NODES}
    base.update(overrides)
    return base


async def test_pipeline_runs_seven_nodes_in_order(tmp_path) -> None:
    async with open_checkpointer(tmp_path / "ckpt.db") as saver:
        graph = build_agent_graph(_nodes(), checkpointer=saver)
        result = await graph.ainvoke(
            {"resume_id": "r1", "jd_text": "需要 python"},
            make_thread_config("order-1", recursion_limit=30),
        )

    # M5 起图上有第八个节点（`generate_item`），但它不被 `traced()` 包：一次扇出派 N 份，
    # 全记进 trace 会把"七个阶段"的观测面冲花。所以这里等号右边仍是 PIPELINE。
    assert [step["node"] for step in result["trace"]] == [name.value for name in PIPELINE]
    assert all(step["ok"] and step["ms"] >= 0 for step in result["trace"])
    assert sum(1 for step in result["trace"] if step["ok"]) == len(PIPELINE)


async def test_node_failure_attaches_a_failed_step_to_the_exception(tmp_path) -> None:
    """`ok=False` 那一步挂在异常上，不写 state：节点抛异常时任何通道都落不了地〔T-008〕

    失败步骤只能由 `traced()` 生成——调用方只拿到异常，拿不到"当时在跑哪个节点"，
    而中止归因要的正是节点名与错误码这两样（`app/tasks/question_generation.py` 取它落成进度步）。
    """

    async def exploding(state: AgentState) -> Mapping[str, Any]:
        raise AppError("半路崩了", code="x_code")

    async with open_checkpointer(tmp_path / "ckpt.db") as saver:
        graph = build_agent_graph(_nodes(validate=exploding), checkpointer=saver)
        with pytest.raises(AppError) as excinfo:
            await graph.ainvoke(
                {"resume_id": "r1"}, make_thread_config("fail-step", recursion_limit=30)
            )

    step = failed_trace_step(excinfo.value)
    assert step is not None
    assert step["node"] == NodeName.VALIDATE.value
    assert step["ok"] is False
    assert step["detail"] == "x_code"


async def test_failure_step_falls_back_to_the_exception_type_name(tmp_path) -> None:
    """非业务异常没有 `code` 可给，用类型名占位：`detail` 恒有值，归因才不会靠猜。"""

    async def exploding(state: AgentState) -> Mapping[str, Any]:
        raise KeyError("plan")

    async with open_checkpointer(tmp_path / "ckpt.db") as saver:
        graph = build_agent_graph(_nodes(profile=exploding), checkpointer=saver)
        with pytest.raises(KeyError) as excinfo:
            await graph.ainvoke({}, make_thread_config("fail-type", recursion_limit=30))

    step = failed_trace_step(excinfo.value)
    assert step is not None and step["detail"] == "KeyError"
    assert step["node"] == NodeName.PROFILE.value


async def test_node_declared_prompt_version_lands_in_trace_only(tmp_path) -> None:
    async def parse(state: AgentState) -> Mapping[str, Any]:
        return {"raw_text": "简历正文", "prompt_version": "v9"}

    async with open_checkpointer(tmp_path / "ckpt.db") as saver:
        graph = build_agent_graph(_nodes(parse=parse), checkpointer=saver)
        result = await graph.ainvoke({}, make_thread_config("trace-version", recursion_limit=30))

    step = next(s for s in result["trace"] if s["node"] == NodeName.PARSE.value)
    assert step["prompt_version"] == "v9"
    assert "prompt_version" not in result, "保留键只用于 trace，不许进通道"
    assert result["raw_text"] == "简历正文"


async def test_missing_node_fails_at_assembly() -> None:

    nodes = _nodes()
    del nodes[NodeName.REVIEW]
    with pytest.raises(ValueError, match="缺少节点实现"):
        build_agent_graph(nodes)


async def test_review_is_terminal_even_with_issues(tmp_path) -> None:
    """ADR-0015：没拿到授权的运行里，`review` 就是终点——批注再多也只扇一轮。

    ADR-0021 开的例外是**授权制**的，所以这条用例的输入里没有 `auto_fix`：
    默认形状必须仍然是 `generate → review → END`。
    """

    async def review(state: AgentState) -> Mapping[str, Any]:
        return {"review_issues": [{"index": 0, "problem": "与简历无关"}], "finished": True}

    async with open_checkpointer(tmp_path / "ckpt.db") as saver:
        graph = build_agent_graph(_nodes(review=review), checkpointer=saver)
        result = await graph.ainvoke(
            {"questions": [{"stem": "Q"}]}, make_thread_config("no-loop", recursion_limit=30)
        )

    ran = [step["node"] for step in result["trace"]]
    assert ran.count(NodeName.REVIEW.value) == 1
    assert ran.count(NodeName.GENERATE.value) == 1
    assert ran[-1] == NodeName.REVIEW.value
    assert result["review_issues"], "批注留在通道里，只作观测"


@pytest.mark.parametrize("auto_fix", [False, True])
async def test_authorized_review_loops_back_exactly_once(tmp_path, auto_fix: bool) -> None:
    """`review → generate` 这张边的去留只看授权〔T-404 / ADR-0021〕，上限是**一轮**。

    旧的质量门按"批注占比"决定要不要回炉，新路由一个比例都不算：`auto_fix=false` 恒回 END，
    `true` 且有点名的题就回一次。批次号 `!= 1` 就直接停，所以成本上界是"多一轮扇出"，
    而不是受 `recursion_limit`（这里是 30）约束——那两个数字差一个量级。
    """

    async def generate(state: AgentState) -> Mapping[str, Any]:
        batch = int(state.get("question_batch") or 0) + 1
        row = {"stem": f"第 {batch} 版", "batch": batch, "order_index": 0}
        return {
            "question_batch": batch,
            "plan": {"items": [{"topic": "并发", "difficulty": "medium", "count": 1}], "total": 1},
            # 占位节点不扇分支，所以题自己写：`fanout_slots` 缺省时路由直落 `review`
            "questions": Overwrite(value=[row]),
        }

    async def review(state: AgentState) -> Mapping[str, Any]:
        return {
            "review_issues": [{"index": 0, "problem": "题干信息不全", "fix": "补上数据量级"}],
            "finished": True,
        }

    async with open_checkpointer(tmp_path / "ckpt.db") as saver:
        graph = build_agent_graph(_nodes(generate=generate, review=review), checkpointer=saver)
        result = await graph.ainvoke(
            {"auto_fix": auto_fix}, make_thread_config("redo-loop", recursion_limit=30)
        )

    ran = [step["node"] for step in result["trace"]]
    rounds = 2 if auto_fix else 1
    assert ran.count(NodeName.GENERATE.value) == rounds
    assert ran.count(NodeName.REVIEW.value) == rounds
    assert result["question_batch"] == rounds
    assert ran[-1] == NodeName.REVIEW.value, "无论修不修，收尾都在 review"


async def test_validate_loop_retries_parse_then_advances(tmp_path) -> None:
    agent = get_settings().agent

    async def validate(state: AgentState) -> Mapping[str, Any]:
        retries = state.get("validate_retries", 0) + 1
        return {"validate_retries": retries, "validate_issues": ["缺少毕业年份"]}

    async with open_checkpointer(tmp_path / "ckpt.db") as saver:
        graph = build_agent_graph(_nodes(validate=validate), checkpointer=saver)
        result = await graph.ainvoke({}, make_thread_config("loop-2", recursion_limit=30))

    ran = [step["node"] for step in result["trace"]]
    assert ran.count(NodeName.PARSE.value) == agent.validate_max_retries + 1
    assert ran.count(NodeName.VALIDATE.value) == agent.validate_max_retries + 1
    assert NodeName.MATCH.value in ran


async def test_checkpoint_accumulates_across_two_runs_on_same_thread(tmp_path) -> None:
    db = tmp_path / "ckpt.db"
    async with open_checkpointer(db) as saver:
        graph = build_agent_graph(_nodes(), checkpointer=saver)
        first = await graph.ainvoke({}, make_thread_config("resume-thread", recursion_limit=30))
        second = await graph.ainvoke({}, make_thread_config("resume-thread", recursion_limit=30))

    assert len(first["trace"]) == 7
    assert len(second["trace"]) == 14, "同 thread_id 的第二次运行应在 checkpoint 上继续累积"


async def test_read_thread_state_locates_one_thread_without_scanning(tmp_path) -> None:
    """T-304：报告行自己记着线程 id，于是"取回那次运行的图终态"是一次定位。

    旧路是扫全部线程再按 `interview_id` 认（`export_report_md.py` 以前就这么干），
    在实测 21.77MB 的 checkpoint 库上每导一次都要走一遍。这里钉三件事：
    能取回、取的是那一个、线程不存在与线程 id 为空都只回空 dict。
    """
    db = tmp_path / "ckpt.db"

    async def write_parsed(state: AgentState) -> Mapping[str, Any]:
        return {"parsed": {"name": "林可"}, "interview_id": "iv-target"}

    async with open_checkpointer(db) as saver:
        graph = build_agent_graph(_nodes(parse=write_parsed), checkpointer=saver)
        await graph.ainvoke({}, make_thread_config("thread-304", recursion_limit=30))
        await graph.ainvoke({}, make_thread_config("other-thread", recursion_limit=30))

    values = await read_thread_state("thread-304", checkpoint_path=db)
    assert values["interview_id"] == "iv-target"
    assert values["parsed"] == {"name": "林可"}
    # 只读的壳图不跑节点：读回来的是 channel 值，trace 是那一次运行留下的，不是新写的
    assert [step["node"] for step in values["trace"]] == [name.value for name in PIPELINE]

    assert await read_thread_state("no-such-thread", checkpoint_path=db) == {}
    # 空 id（旧报告的 `thread_id` 列）直接回空 dict：不猜线程、也不报错
    assert await read_thread_state("", checkpoint_path=tmp_path / "gone" / "c.db") == {}


class _Stem(BaseModel):
    stem: str


def _conn_error() -> APIConnectionError:
    return APIConnectionError(
        message="conn",
        request=httpx.Request("POST", "https://api.deepseek.com"),
    )


def _fake_client(replies: list[Any]) -> FakeCompletions:
    """注入替身。三种失败玩法（`raises` / 列表里放异常 / 按序吐串）见 `_fake_llm` 的 docstring。"""
    return FakeCompletions(replies)


async def test_chat_json_rejects_and_repairs_with_feedback() -> None:
    fake = _fake_client(["这不是 JSON", '{"stem": "讲讲你的项目"}'])
    client = LLMClient(client=fake)

    result = await client.chat_json(
        "extract", [{"role": "user", "content": "出题"}], schema=_Stem, max_attempts=2
    )

    assert isinstance(result, _Stem)
    assert len(fake.calls) == 2
    assert fake.messages(1)[0]["content"] == "出题"
    assert fake.messages(1)[1]["role"] == "assistant", "重问必须带上模型上次的原始输出"
    assert "不合格" in fake.messages(1)[2]["content"]
    assert client.usage.calls == 2
    assert client.usage.prompt_tokens == 20


async def test_chat_json_gives_up_after_attempts() -> None:
    client = LLMClient(client=_fake_client(["{}", "not json"] * 3))
    with pytest.raises(LLMResponseError, match="未产出合规 JSON"):
        await client.chat_json(
            "review", [{"role": "user", "content": "审查"}], schema=_Stem, max_attempts=2
        )


async def test_chat_retries_transport_error_then_succeeds() -> None:
    boom = _conn_error()
    fake = _fake_client([boom, "hello"])
    client = LLMClient(client=fake)

    assert await client.chat("generate", [{"role": "user", "content": "hi"}]) == "hello"
    assert len(fake.calls) == 2
    assert client.usage.failed_calls == 0


async def test_chat_raises_llm_error_when_retries_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings.llm, "transport_max_attempts", 2)
    boom = _conn_error()
    fake = _fake_client([boom])
    client = LLMClient(settings=settings, client=fake)

    with pytest.raises(LLMError, match="模型 .* 调用失败"):
        await client.chat("extract", [{"role": "user", "content": "hi"}])
    assert len(fake.calls) == 2
    assert client.usage.failed_calls == 1
