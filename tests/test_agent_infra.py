"""LangGraph 基础设施与 LLM 客户端的冒烟测试（阶段 0-4 的验收替身节点）。"""

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import APIConnectionError
from pydantic import BaseModel

from app.agents.state import GRAPH_NODES, PIPELINE, AgentState, NodeName
from app.agents.workflow import build_agent_graph, make_thread_config, open_checkpointer
from app.config import get_settings
from app.exceptions.base import LLMError, LLMResponseError
from app.utils.llm_client import LLMClient


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
    """ADR-0015：`review` 后面没有回退边了——批注再多，一次运行也只扇一轮 `generate`。"""

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


class _Stem(BaseModel):
    stem: str


def _conn_error() -> APIConnectionError:
    return APIConnectionError(
        message="conn",
        request=httpx.Request("POST", "https://api.deepseek.com"),
    )


class _FakeCompletions:
    """替身：既当作 client 注入（`chat.completions.create`），也记录每次的 messages。"""

    def __init__(self, replies: list[Any]) -> None:
        self.replies = replies
        self.calls: list[list[dict[str, str]]] = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(list(kwargs["messages"]))
        reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        if isinstance(reply, BaseException):
            raise reply
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=reply))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )


def _fake_client(replies: list[Any]) -> _FakeCompletions:
    return _FakeCompletions(replies)


async def test_chat_json_rejects_and_repairs_with_feedback() -> None:
    fake = _fake_client(["这不是 JSON", '{"stem": "讲讲你的项目"}'])
    client = LLMClient(client=fake)

    result = await client.chat_json(
        "extract", [{"role": "user", "content": "出题"}], schema=_Stem, max_attempts=2
    )

    assert isinstance(result, _Stem)
    assert len(fake.calls) == 2
    assert fake.calls[1][0]["content"] == "出题"
    assert fake.calls[1][1]["role"] == "assistant", "重问必须带上模型上次的原始输出"
    assert "不合格" in fake.calls[1][2]["content"]
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
