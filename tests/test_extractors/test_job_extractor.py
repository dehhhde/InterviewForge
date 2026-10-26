"""job_extractor 的单测：规则白拿的三项 + LLM 兜底的出处核对（全程离线）。

只断言值、来源、缺口与请求形状，不断言模型文案——回复是预置的。

这里最要紧的是"什么不算认出来"：岗位名是 `job` 表的自然键一半，公司名会印在报告头上，
两者认错都比认不出更糟。所以每个用例都在钉丢弃条件，而不是钉命中数量。
"""

import json
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest

from app.extractors.job_extractor import JobFieldExtractor, extract_by_rules
from app.utils.llm_client import LLMClient

#: 中文 JD 的理想形状：三项都带字形，规则层全包
_LABELED = "岗位名称：后端开发工程师\n公司名称：示例科技有限公司\n任职要求：3-5 年工作经验"
#: 海报形状：没有标签，标题在第一行，公司在落款
_POSTER = "【高级后端工程师】Base 上海\n某某研究院 招聘\n经验：5 年以上"


class _FakeCompletions:
    """替身：按顺序吐预置回复，记下每次 `create` 的 kwargs。"""

    def __init__(self, replies: Sequence[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        text = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )


def _extractor(replies: Sequence[str]) -> tuple[JobFieldExtractor, _FakeCompletions]:
    fake = _FakeCompletions(replies)
    return JobFieldExtractor(llm=LLMClient(client=fake)), fake


def _span_json(**fields: tuple[str, str] | None) -> str:
    """拼模型回复：`title=("后端工程师", "高级后端工程师")`；给 None 表示模型答 null。"""
    payload: dict[str, dict[str, str]] = {}
    for key, span in fields.items():
        if span is not None:
            payload[key] = {"value": span[0], "evidence": span[1]}
    return json.dumps(payload, ensure_ascii=False)


def test_rules_take_the_free_fields() -> None:
    result = extract_by_rules(_LABELED)

    assert (result.title.value, result.title.source) == ("后端开发工程师", "regex")
    assert result.company.value == "示例科技有限公司"
    assert result.min_years.value == "3", "区间取下界：3-5 年说的是至少 3 年"
    assert result.prompt_version == "", "没花模型调用就不该有模板版本"
    assert result.missing == ()


def test_heading_layout_without_any_label() -> None:
    result = extract_by_rules(_POSTER)

    assert result.title.value == "高级后端工程师", "括号后的 Base 上海是补充说明，不进自然键"
    assert result.title.evidence == "【高级后端工程师】Base 上海", "证据保留原文写法"
    assert result.company.value == "某某研究院"
    assert result.min_years.value == "5"


def test_years_demand_an_experience_line() -> None:
    """公司成立年份、合同期都不是期望年限；整段匹配会把它们串成一场误会。"""
    assert extract_by_rules("成立于 2015 年，员工 3000 人").min_years is None
    assert extract_by_rules("签 3 年合同").min_years is None
    assert extract_by_rules("从业 5 年，Java 后端").min_years.value == "5"


def test_absurd_years_are_dropped_not_clamped() -> None:
    """OCR 把薪资糊进年限行会出"61 年以上"，宁可留空也别存一个 61。"""
    assert extract_by_rules("任职要求：61 年以上经验").min_years is None


def test_list_item_is_not_a_title() -> None:
    """海报没有标题行时，"1. 负责数据平台开发"也带"开发"，但它是列表项。"""
    result = extract_by_rules(
        "工作地点：上海市浦东新区\n【岗位职责】\n1. 负责数据平台开发；\n任职要求：2 年经验"
    )

    assert result.title is None
    assert result.min_years is not None, "年限这行不受标题判据影响"


def test_title_never_a_section_heading() -> None:
    assert extract_by_rules("岗位名称：\n岗位职责：负责网关").title is None


async def test_llm_fallback_is_asked_only_for_the_gaps() -> None:
    """规则认出岗位名，公司名没有后缀词典可依——那一次调用只该问 company。"""
    jd = "岗位名称：后端工程师\n招聘方是某电商平台\n要求：5 年以上 Java 经验"
    extractor, fake = _extractor([_span_json(company=("某电商平台", "某电商平台"))])

    result = await extractor.extract(jd)

    assert (result.company.value, result.company.source) == ("某电商平台", "llm")
    assert result.title.value == "后端工程师", "规则层的结果不被兜底覆盖"
    assert result.prompt_version == "job_fields@v1"
    assert result.missing == ()
    assert len(fake.calls) == 1
    sent = fake.calls[0]["messages"]
    assert "company" in sent[-1]["content"] and "title" not in sent[-1]["content"]


async def test_no_llm_call_when_rules_cover_title_and_company() -> None:
    extractor, fake = _extractor([_span_json(company=("不该被用到", "不该被用到"))])

    await extractor.extract(_LABELED)

    assert fake.calls == []


async def test_fallback_request_uses_extract_route_and_json_mode() -> None:
    extractor, fake = _extractor([_span_json(title=("后端", "后端"))])

    await extractor.extract("任职要求：5 年以上经验")

    kwargs = fake.calls[0]
    assert kwargs["model"] == extractor.settings.llm_model_extract
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["temperature"] == 0.0


async def test_value_without_a_place_in_the_original_is_dropped() -> None:
    """证据不在原文里 = 幻觉：岗位名留空，让页面去问人。"""
    jd = "任职要求：5 年以上经验"
    extractor, fake = _extractor([_span_json(title=("超级后端工程师", "本公司招聘超级后端工程师"))])

    result = await extractor.extract(jd)

    assert result.title is None
    assert result.missing == ("title", "company")
    assert any("岗位名称" in note for note in result.notes)
    assert len(fake.calls) == 1


async def test_value_must_sit_inside_its_own_evidence() -> None:
    """证据真在原文、值却被模型"概括"过——那是换了个岗位，照样丢。"""
    jd = (
        "某某科技有限公司 长期招聘\n"
        "职位描述见附件，我们正在寻找资深后端开发工程师\n"
        "任职要求：5 年以上经验"
    )
    extractor, fake = _extractor([_span_json(title=("架构师", "资深后端开发工程师"))])

    result = await extractor.extract(jd)

    assert result.title is None, "证据核得过，但值不在证据里"
    assert result.company.value == "某某科技有限公司"
    assert result.missing == ("title",)
    assert len(fake.calls) == 1


async def test_model_answering_null_leaves_the_field_blank() -> None:
    jd = "任职要求：5 年以上经验"
    extractor, fake = _extractor([_span_json(title=None, company=None)])

    result = await extractor.extract(jd)

    assert result.title is None and result.company is None
    assert set(result.missing) == {"title", "company"}


def test_unknown_prompt_node_is_a_configuration_error() -> None:
    """模板目录被挪走时要在调用前炸，而不是静默换一版模板。"""
    from app.prompts.registry import UnknownPromptError, get_prompt

    assert get_prompt("job_fields").node == "job_fields"
    with pytest.raises(UnknownPromptError):
        get_prompt("job_fields", "v999")
