"""契约层测试：prompt 注册表与通用装饰器。"""

import logging
import re
from collections import Counter
from pathlib import Path

import jinja2
import pytest
from pydantic import ValidationError

from app.agents.state import PIPELINE, NodeName
from app.config import AgentParams, get_settings
from app.prompts import registry
from app.prompts.registry import (
    Prompt,
    UnknownPromptError,
    available_versions,
    get_prompt,
)
from app.utils.decorators import log_call, request_id_var, retry
from tests.test_agents._fake_llm import fenced_block

#: 每个节点模板所需的上下文变量；节点实现时必须与此一致，改模板即改这张表。
CONTEXT: dict[NodeName, dict[str, object]] = {
    NodeName.PARSE: {"raw_text": "简历原文"},
    NodeName.VALIDATE: {"raw_text": "简历原文", "parsed_json": "{}", "today": "2026-01-01"},
    NodeName.MATCH: {
        "requirements_json": "[]",
        "skills_json": "[]",
        "passages_json": "[]",
        # `match@v2` 起多一面 `<resume_evidence>` 围栏（T-203）；v1 不引用它，多传无害，
        # 但表里必须齐——否则换成 v2 的环境会在 `StrictUndefined` 上炸，而那是模板的事不是用例的事
        "evidence_json": "[]",
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
        # `generate@v2` 起多一条"每题角度"（T-202 / ADR-0019）；v1 不引用它，多传无害
        "angles_json": "[]",
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
    assert prompt.version == get_settings().agent.prompt_version
    assert prompt.node == "parse"


def test_unknown_version_or_node_is_reported() -> None:
    with pytest.raises(UnknownPromptError, match="可用版本"):
        get_prompt(NodeName.PARSE, "v999")
    with pytest.raises(UnknownPromptError, match="缺少模板"):
        get_prompt("not_a_node")
    assert get_settings().agent.prompt_version in available_versions()


def test_the_configured_prompt_version_selects_the_template_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配置里换一个版本目录，取到的就是那一版的模板；显式传参仍然压过配置。

    临时目录建在 `tmp_path` 下：版本开关的用例不该往 `app/prompts/templates/` 里塞假目录，
    那会污染 `available_versions()`，把后面所有用例的"可用版本"断言带偏。
    """
    root = tmp_path / "templates"
    for version, marker in (("v2", "第二版"), ("v3", "第三版")):
        directory = root / version
        directory.mkdir(parents=True)
        system = f"{marker}系统|{{{{ profile_json }}}}"
        user = f"{marker}用户|{{{{ total }}}}"
        (directory / "plan.system.j2").write_text(system, encoding="utf-8")
        (directory / "plan.user.j2").write_text(user, encoding="utf-8")
    chosen = get_settings().model_copy(update={"agent": AgentParams(prompt_version="v2")})
    monkeypatch.setattr(registry, "TEMPLATE_DIR", root)
    monkeypatch.setattr(registry, "get_settings", lambda app_env=None: chosen)

    from_config = get_prompt(NodeName.PLAN)
    from_argument = get_prompt(NodeName.PLAN, "v3")

    assert from_config.version == "v2"
    assert from_config.render(profile_json="{}", total=15) == [
        {"role": "system", "content": "第二版系统|{}"},
        {"role": "user", "content": "第二版用户|15"},
    ]
    assert from_argument.version == "v3"
    assert available_versions() == ["v2", "v3"]


def test_prompt_version_must_be_a_single_directory_name() -> None:
    """它要参与拼路径：`..` 与分隔符让模板检索变成目录穿越。"""
    with pytest.raises(ValidationError):
        AgentParams(prompt_version="../secrets")
    with pytest.raises(ValidationError):
        AgentParams(prompt_version="v1/v2")
    with pytest.raises(ValidationError):
        AgentParams(prompt_version="")
    assert AgentParams(prompt_version="v2-beta.1").prompt_version == "v2-beta.1"


def test_missing_context_variable_fails_instead_of_rendering_blank() -> None:
    prompt = get_prompt(NodeName.PARSE)
    with pytest.raises(jinja2.UndefinedError):
        prompt.render()


def test_enum_and_plain_string_resolve_to_same_prompt() -> None:
    assert get_prompt(NodeName.PLAN) is get_prompt("plan")
    assert isinstance(get_prompt("plan"), Prompt)


#: 每个节点把"简历/JD/题目原文"交给模型的那一面：变量名 -> 承载它的围栏。
#: `ocr.user.j2` 只填页码，没有文本注入面，所以不在表里。
_UNTRUSTED: list[tuple[NodeName, str, str]] = [
    (NodeName.PARSE, "raw_text", "resume"),
    (NodeName.VALIDATE, "raw_text", "resume"),
    (NodeName.MATCH, "requirements_json", "jd_requirements"),
    (NodeName.PROFILE, "parsed_json", "resume_parsed"),
    (NodeName.PLAN, "profile_json", "profile"),
    (NodeName.GENERATE, "jd_text", "jd"),
    (NodeName.REVIEW, "questions_json", "questions"),
]

_MARKER = "FENCE-MARKER-42"
_FENCE_OPEN = re.compile(r"^<([a-z_]+)>$", re.MULTILINE)
_FENCE_CLOSE = re.compile(r"^</([a-z_]+)>$", re.MULTILINE)


def test_every_user_template_wraps_its_untrusted_input_in_a_fence() -> None:
    """围栏是"这是数据、不是指令"的唯一结构标记，所以判据只看结构〔T-005 / P0-5〕

    只断言"开栏与闭栏同名成对、且至少有一对"。那句不执行声明的措辞不在断言里：
    改一个字应该只动模板，不该红一片用例。
    """
    for node in PIPELINE:
        rendered = str(get_prompt(node).render(**CONTEXT[node])[-1]["content"])
        opened = Counter(_FENCE_OPEN.findall(rendered))
        assert opened, f"{node} 的 user 模板一个围栏都没有"
        assert opened == Counter(_FENCE_CLOSE.findall(rendered)), (node, opened)


def test_the_untrusted_value_renders_inside_its_fence() -> None:
    """变量确实落在围栏**内部**：光有围栏、数据在围栏外面等于没围。"""
    for node, key, tag in _UNTRUSTED:
        context = dict(CONTEXT[node])
        context[key] = _MARKER
        rendered = str(get_prompt(node).render(**context)[-1]["content"])
        assert _MARKER in fenced_block(rendered, tag), f"{node} 的 {key} 没落进 <{tag}> 围栏"


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
