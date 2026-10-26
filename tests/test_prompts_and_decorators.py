"""契约层测试：prompt 注册表与通用装饰器。"""

import logging

import jinja2
import pytest

from app.agents.state import PIPELINE, NodeName
from app.prompts.registry import (
    DEFAULT_VERSION,
    Prompt,
    UnknownPromptError,
    available_versions,
    get_prompt,
)
from app.utils.decorators import log_call, request_id_var, retry

#: 每个节点模板所需的上下文变量；节点实现时必须与此一致，改模板即改这张表。
CONTEXT: dict[NodeName, dict[str, object]] = {
    NodeName.PARSE: {"raw_text": "简历原文"},
    NodeName.VALIDATE: {"raw_text": "简历原文", "parsed_json": "{}", "today": "2026-01-01"},
    NodeName.MATCH: {
        "requirements_json": "[]",
        "skills_json": "[]",
        "passages_json": "[]",
    },
    NodeName.PROFILE: {
        "rubric_json": "{}",
        "parsed_json": "{}",
        "matched_gaps_json": "[]",
    },
    NodeName.PLAN: {"profile_json": "{}", "total": 15, "difficulty_ratio_json": "{}"},
    NodeName.GENERATE: {
        "parsed_json": "{}",
        "jd_text": "负责高并发服务的容量规划",
        "roster_json": "[]",
        "difficulty": "medium",
        "source": "resume",
        "topic": "分布式缓存",
        "count": 2,
    },
    NodeName.REVIEW: {"parsed_json": "{}", "jd_text": "负责容量规划", "questions_json": "[]"},
}


def test_every_node_has_a_renderable_template() -> None:
    for node in PIPELINE:
        prompt = get_prompt(node)
        messages = prompt.render(**CONTEXT[node])
        assert [m["role"] for m in messages] == ["system", "user"]
        assert all(len(m["content"]) > 20 for m in messages), node


def test_prompt_version_is_exposed_for_tracing() -> None:
    prompt = get_prompt(NodeName.PARSE)
    assert prompt.version == DEFAULT_VERSION
    assert prompt.node == "parse"


def test_unknown_version_or_node_is_reported() -> None:
    with pytest.raises(UnknownPromptError, match="可用版本"):
        get_prompt(NodeName.PARSE, "v999")
    with pytest.raises(UnknownPromptError, match="缺少模板"):
        get_prompt("not_a_node")
    assert DEFAULT_VERSION in available_versions()


def test_missing_context_variable_fails_instead_of_rendering_blank() -> None:
    prompt = get_prompt(NodeName.PARSE)
    with pytest.raises(jinja2.UndefinedError):
        prompt.render()


def test_enum_and_plain_string_resolve_to_same_prompt() -> None:
    assert get_prompt(NodeName.PLAN) is get_prompt("plan")
    assert isinstance(get_prompt("plan"), Prompt)


@log_call
async def _logged(value: int) -> str:
    return f"结果 {value}"


async def test_log_call_records_request_id_and_duration(caplog: pytest.LogCaptureFixture) -> None:
    token = request_id_var.set("req-42")
    try:
        with caplog.at_level(logging.INFO, logger="app.call"):
            assert await _logged(7) == "结果 7"
    finally:
        request_id_var.reset(token)

    text = caplog.text
    assert "req-42" in text
    assert "_logged 开始" in text
    assert "耗时=" in text


def test_log_call_truncates_long_arguments(caplog: pytest.LogCaptureFixture) -> None:
    @log_call
    def _echo(text: str) -> str:
        return text

    with caplog.at_level(logging.INFO, logger="app.call"):
        _echo("X" * 500)
    assert "…(共 " in caplog.text
    assert "X" * 200 not in caplog.text


@log_call
async def _boom() -> None:
    raise RuntimeError("节点炸了")


async def test_log_call_logs_failure_and_reraises(caplog: pytest.LogCaptureFixture) -> None:
    with (
        caplog.at_level(logging.INFO, logger="app.call"),
        pytest.raises(RuntimeError, match="节点炸了"),
    ):
        await _boom()
    assert "_boom 失败" in caplog.text


class _Flaky:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    @retry(max_attempts=3, retry_on=(TimeoutError,))
    async def run(self) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise TimeoutError("网络抖动")
        return "ok"


async def test_retry_succeeds_within_budget() -> None:
    flaky = _Flaky(1)
    assert await flaky.run() == "ok"
    assert flaky.calls == 2


async def test_retry_reraises_after_last_attempt() -> None:
    flaky = _Flaky(5)
    with pytest.raises(TimeoutError):
        await flaky.run()
    assert flaky.calls == 3


async def test_retry_does_not_swallow_other_exceptions() -> None:
    class _Boom:
        @retry(max_attempts=3, retry_on=(TimeoutError,))
        async def run(self) -> None:
            raise ValueError("不在重试白名单里")

    with pytest.raises(ValueError, match="不在重试白名单里"):
        await _Boom().run()
