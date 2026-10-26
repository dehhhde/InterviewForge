"""ner_extractor 的单测：正则白拿的字段 + LLM 兜底的证据核对（全程离线）。

只断言结构、来源、请求形状与失败码，不断言模型文案本身——回复是预置的。
"""

import json
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.extractors.ner_extractor import NerExtractor, extract_by_regex
from app.parsers.pdf_parser import PdfParser
from app.utils.llm_client import LLMClient

REAL_RESUME = Path(__file__).parent.parent / "fixtures" / "resumes" / "附件简历-梁清生.pdf"

#: 有标签、有一切正则字形的理想文本
_LABELED = "姓名：陈昊\n电话 +86 138-0013-8000 | chen_hao@example.com\n3年工作经验"
#: 真简历的形状：姓名裸奔在第一行，没有任何标签
_UNLABELED = "梁清生\n男 | 17626460250 | 3405628625@qq.com\n2年工作经验"


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


def _extractor(replies: Sequence[str]) -> tuple[NerExtractor, _FakeCompletions]:
    fake = _FakeCompletions(replies)
    return NerExtractor(llm=LLMClient(client=fake)), fake


def _span_json(**fields: tuple[str, str] | None) -> str:
    """拼模型回复：`name=("梁清生", "梁清生")`；给 None 表示模型答 null。"""
    payload: dict[str, dict[str, str]] = {}
    for key, span in fields.items():
        if span is not None:
            payload[key] = {"value": span[0], "evidence": span[1]}
    return json.dumps(payload, ensure_ascii=False)


def test_regex_layer_wins_the_free_fields() -> None:
    result = extract_by_regex(_LABELED)

    assert (result.name.value, result.name.source) == ("陈昊", "regex")
    assert result.phone.value == "13800138000"
    assert result.phone.evidence == "+86 138-0013-8000", "证据要保留原文写法"
    assert result.email.value == "chen_hao@example.com"
    assert result.years_experience.value == "3"
    assert result.as_resume_fields() == {
        "name": "陈昊",
        "email": "chen_hao@example.com",
        "phone": "13800138000",
        "years_experience": 3.0,
    }


def test_phone_pattern_rejects_id_card_and_odd_digit_runs() -> None:
    # 18 位身份证也以 1 开头，不挡住就会当成手机号
    assert extract_by_regex("身份证 110101199003074561").phone is None
    assert extract_by_regex("号码 138001380001").phone is None
    assert extract_by_regex("热线 1380013").phone is None


def test_years_pattern_demands_experience_context() -> None:
    assert extract_by_regex("教育 2021-2025 在校").years_experience is None
    assert extract_by_regex("签 3 年合同").years_experience is None
    assert extract_by_regex("从业 5 年，Java 后端").years_experience.value == "5"


def test_name_needs_an_explicit_label() -> None:
    """裸奔的姓名交给 LLM：按"第一行是两个字"猜，会把"男"或"简历"读成姓名。"""
    assert extract_by_regex(_UNLABELED).name is None
    assert extract_by_regex(_LABELED).name is not None


async def test_llm_fallback_is_asked_only_for_the_gaps() -> None:
    extractor, fake = _extractor([_span_json(name=("梁清生", "梁清生"))])

    result = await extractor.extract(_UNLABELED)

    assert (result.name.value, result.name.source) == ("梁清生", "llm")
    assert result.prompt_version == "ner@v1"
    assert result.missing == ()
    assert len(fake.calls) == 1
    sent = fake.calls[0]["messages"]
    assert sent[0]["role"] == "system" and isinstance(sent[0]["content"], str)
    user = sent[-1]["content"]
    assert "name" in user and "梁清生" in user, "只该问缺的字段，并附上原文"
    assert _LABELED.splitlines()[0] not in user


async def test_fallback_request_uses_extract_route_and_json_mode() -> None:
    extractor, fake = _extractor([_span_json(name=("梁清生", "梁清生"))])
    await extractor.extract(_UNLABELED)

    kwargs = fake.calls[0]
    assert kwargs["model"] == extractor.settings.llm_model_extract
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["temperature"] == 0.0


async def test_no_llm_call_when_regex_covers_everything() -> None:
    extractor, fake = _extractor([_span_json(name=("陈昊", "陈昊"))])

    result = await extractor.extract(_LABELED)

    assert fake.calls == []
    assert result.prompt_version == ""
    assert result.notes == ()


async def test_evidence_not_in_the_original_is_treated_as_hallucination() -> None:
    extractor, _ = _extractor([_span_json(name=("梁清生", "这是简历里的梁清生本人"))])

    result = await extractor.extract(_UNLABELED)

    assert result.name is None
    assert result.missing == ("name",)
    assert any("name" in note for note in result.notes)


async def test_evidence_check_ignores_whitespace_reflow() -> None:
    """PDF 排版残留：原文"2年   工作经验"，模型抄成"2年 工作经验"不算幻觉。"""
    text = "梁清生\n2年   工作经验"
    extractor, _ = _extractor([_span_json(name=("梁清生", "梁清生"))])

    result = await extractor.extract(text)

    assert result.name.value == "梁清生"


async def test_payload_without_any_span_leaves_fields_missing() -> None:
    extractor, fake = _extractor(["{}"])

    result = await extractor.extract("只有一句话的简历")

    assert result.missing == ("name", "email", "phone", "years_experience")
    assert len(fake.calls) == 1, "空对象是合法 JSON，不该触发校验重试"


def test_real_resume_contact_fields_are_free(tmp_path: Path) -> None:
    if not REAL_RESUME.is_file():
        pytest.skip("缺少真实简历样本")

    document = PdfParser().parse(REAL_RESUME)
    result = extract_by_regex(document.raw_text)

    assert result.phone.value == "17626460250"
    assert result.email.value == "3405628625@qq.com"
    assert result.years_experience.value == "2"
    assert result.name is None, "真简历第一行只有名字，没标签——这正是兜底的活"


#: 计划书 §5 的"录制回放"：真调一次存成 fixture，之后离线跑
REPLAY = Path(__file__).parent.parent / "fixtures" / "llm" / "ner_liang_qingsheng.json"


async def test_replayed_real_response_passes_the_evidence_gate() -> None:
    if not (REPLAY.is_file() and REAL_RESUME.is_file()):
        pytest.skip("缺少录制回放的 fixture")
    recorded = json.loads(REPLAY.read_text(encoding="utf-8"))
    raw_text = PdfParser().parse(REAL_RESUME).raw_text

    extractor, fake = _extractor([recorded["response"]])
    result = await extractor.extract(raw_text)

    assert (result.name.value, result.name.source) == ("梁清生", "llm")
    assert result.prompt_version == f"ner@{recorded['prompt_version']}"
    assert fake.calls[0]["model"] == recorded["model"], "回放要贴着录制时的路由"

