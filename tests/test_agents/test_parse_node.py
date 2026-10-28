"""parse 节点的接线测试：文件定位 → 解析 → 抽取 → `parsed` 通道形状（全程离线）。

断言的是通道路由、失败码与幂等性，不断言模型文案；LLM 回复一律预置。
"""

import json
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from app.agents.nodes import parse_node
from app.agents.nodes.parse_node import (
    REUSED_SOURCE_NOTE,
    default_registry,
    locate_resume_file,
    make_parse_node,
)
from app.agents.state import AgentState
from app.agents.workflow import PROMPT_VERSION_KEY
from app.config import get_settings
from app.exceptions.base import NotFoundError, ValidationFailedError
from app.extractors import project_extractor, skill_extractor, timeline_extractor
from app.parsers.base import ParserRegistry
from app.parsers.docx_parser import DocxParser
from app.parsers.image_parser import IMAGE_SUFFIXES
from app.utils.llm_client import LLMClient

from ._fake_llm import FakeCompletions, stamp

REAL_RESUME = Path(__file__).parent.parent / "fixtures" / "resumes" / "附件简历-梁清生.pdf"

_NAME_REPLY = json.dumps(
    {"name": {"value": "梁清生", "evidence": "梁清生"}}, ensure_ascii=False
)
_OCR_PAGE = "姓名：陈昊 13800138000 chen@example.com 3年工作经验"
#: 四项联系人全都有稳定字形 → 这份简历 parse 一次模型都不打，`new_count()` 才只反映"读取"的开销
_FULL_TEXT = ["姓名：陈昊 3年工作经验", "13800138000 chen@example.com"]


class _CountingRegistry(ParserRegistry):
    """数 `parse` 被调了几次。

    复用判据省的是**读文件**，而"没重读"这件事在 LLM 计数上不一定看得见（正则全命中的
    简历本来一次模型都不打），所以直接数读取次数。
    """

    def __init__(self, parsers: Sequence[DocxParser]) -> None:
        super().__init__(parsers)
        self.reads = 0

    def parse(self, path: Path) -> Any:
        self.reads += 1
        return super().parse(path)


def _node(
    tmp_path: Path,
    replies: Sequence[str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    vision_model: str | None = None,
    registry: ParserRegistry | None = None,
) -> tuple[Any, FakeCompletions]:
    settings = get_settings()
    # settings 是 lru_cache 的单例，必须用 monkeypatch 改，直接赋值会漏给后面的测试
    monkeypatch.setattr(settings, "llm_model_vision", vision_model or "")
    fake = FakeCompletions(replies)
    node = make_parse_node(
        llm=LLMClient(settings=settings, client=fake),
        settings=settings,
        upload_dir=tmp_path,
        registry=registry,
    )
    return node, fake


def _state(resume_id: str) -> AgentState:
    return {"resume_id": resume_id}


def _mount(tmp_path: Path, resume_id: str, source: Path) -> Path:
    target = tmp_path / f"{resume_id}{source.suffix}"
    shutil.copyfile(source, target)
    return target


def _scanned_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    rect = pymupdf.Rect(0, 0, 595, 842)
    page = doc.new_page(width=595, height=842)
    page.insert_image(rect, pixmap=pymupdf.Pixmap(pymupdf.csGRAY, rect))
    doc.save(path)
    doc.close()
    return path


def _docx(path: Path, lines: Sequence[str]) -> Path:
    from docx import Document

    document = Document()
    for line in lines:
        document.add_paragraph(line)
    document.save(path)
    return path


async def test_parse_node_fills_raw_text_and_parsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not REAL_RESUME.is_file():
        pytest.skip("缺少真实简历样本")
    _mount(tmp_path, "r1", REAL_RESUME)
    node, fake = _node(tmp_path, [_NAME_REPLY], monkeypatch)

    update = await node(_state("r1"))

    parsed = update["parsed"]
    assert parsed["name"] == "梁清生"
    assert parsed["phone"] == "17626460250"
    assert parsed["email"] == "3405628625@qq.com"
    assert parsed["years_experience"] == pytest.approx(
        2.1
    ), "年限取时间线复算值，不是简历自称的 2 年"
    assert {s["name"] for s in parsed["skills"]} >= {"Java", "Spring Boot", "MySQL", "Redis"}
    assert all(s["evidence"] for s in parsed["skills"]), "每条技能都得能回到原文"
    assert [w["company"] for w in parsed["work_experiences"]] == ["广东裕太科技有限公司"]
    assert parsed["work_experiences"][0]["start"] == "2024-06-01"
    assert [e["school"] for e in parsed["educations"]] == ["广州应用科技学院"]
    assert [p["name"] for p in parsed["projects"]] == ["OA办公自动化系统", "对象存储中心"]
    assert update["raw_text"].startswith("梁清生")
    assert update["errors"] == []
    assert update[PROMPT_VERSION_KEY] == stamp("ner")
    assert len(fake.calls) == 1, "正则白拿的字段不该再问模型"


async def test_parse_node_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not REAL_RESUME.is_file():
        pytest.skip("缺少真实简历样本")
    _mount(tmp_path, "r1", REAL_RESUME)
    node, _ = _node(tmp_path, [_NAME_REPLY], monkeypatch)

    assert await node(_state("r1")) == await node(_state("r1"))


async def test_the_section_map_is_cut_once_for_the_three_extractors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一次 parse 只切一遍栏目表〔T-208〕：省下的是三抽取器各一遍的正则遍历，回退再乘一遍。

    这条只能数调用次数——共用一份表**不改变任何产出**，所以产出断言证不了省。
    产出不变的验收在 `tests/test_extractors/test_sections.py`，两边合起来才是这张卡。
    """
    cuts: list[str] = []
    for module in (parse_node, timeline_extractor, skill_extractor, project_extractor):
        real = module.split_sections

        def _counted(text: str, *, _real=real, _name=module.__name__) -> Any:
            cuts.append(_name)
            return _real(text)

        monkeypatch.setattr(module, "split_sections", _counted)
    _docx(
        tmp_path / "r1.docx",
        [
            "姓名：陈昊 3年工作经验",
            "13800138000 chen@example.com",
            "工作经历",
            "甲公司 Java 后端 2020-01 至 2023-06",
            "项目经历",
            "订单中心 2022-03 至 2023-01",
            "专业技能",
            "熟悉 Redis、MySQL",
        ],
    )
    node, _ = _node(tmp_path, [_NAME_REPLY], monkeypatch)

    update = await node(_state("r1"))

    assert cuts == ["app.agents.nodes.parse_node"], f"栏目表切了 {len(cuts)} 次：{cuts}"
    assert update["parsed"]["work_experiences"], "计数生效的前提是三个抽取器真的都跑了"


async def test_parse_node_pads_years_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _docx(tmp_path / "r2.docx", ["姓名：陈昊", "13800138000 chen@example.com"])
    node, fake = _node(
        tmp_path, [json.dumps({"years_experience": None}, ensure_ascii=False)], monkeypatch
    )

    update = await node(_state("r2"))

    assert update["parsed"]["years_experience"] == 0.0
    assert any("年限按 0 占位" in note for note in update["errors"])
    assert len(fake.calls) == 1, "缺的字段只问一次"
    assert "years_experience" in fake.calls[0]["messages"][-1]["content"], "只问缺的那项"
    assert update[PROMPT_VERSION_KEY] == stamp("ner")


async def test_a_claimed_year_count_that_defies_the_timeline_is_overridden_with_a_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """自称十年、时间线只有两个月：取算出来的那个，并把分歧留给校验层。"""
    _docx(
        tmp_path /
        "r5.docx",
        ["姓名：陈昊", "13年工作经验", "工作经历", "某公司 后端开发 2024.11-2025.01"],
    )
    node, _ = _node(tmp_path, ["{}"], monkeypatch)

    update = await node(_state("r5"))

    assert update["parsed"]["years_experience"] == pytest.approx(0.2)
    assert any("请校验层判是否自洽" in note for note in update["errors"])


async def test_parse_node_routes_scanned_pdf_to_the_vision_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scanned_pdf(tmp_path / "r3.pdf")
    node, fake = _node(
        tmp_path, [_OCR_PAGE, _NAME_REPLY], monkeypatch, vision_model="fake-vision-model"
    )

    update = await node(_state("r3"))

    assert len(fake.calls) == 1, "OCR 文本里正则全命中，无需二次兜底"
    assert fake.calls[0]["model"] == "fake-vision-model"
    assert update["raw_text"] == _OCR_PAGE
    assert update["parsed"]["name"] == "陈昊"
    assert update[PROMPT_VERSION_KEY] == stamp("ocr")


async def test_parse_node_reports_a_missing_file_as_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node, fake = _node(tmp_path, [_NAME_REPLY], monkeypatch)

    with pytest.raises(NotFoundError) as exc:
        await node(_state("ghost"))
    assert exc.value.code == "parse_file_missing"
    assert fake.calls == []


@pytest.mark.parametrize("resume_id", ["", "../etc/passwd", "a/b", ".hidden"])
async def test_locate_rejects_ids_that_could_escape_the_upload_dir(
    tmp_path: Path, resume_id: str
) -> None:
    with pytest.raises(ValidationFailedError) as exc:
        locate_resume_file(tmp_path, resume_id, (".pdf",))
    assert exc.value.code == "parse_bad_resume_id"


async def test_parse_node_gives_up_instead_of_publishing_an_anonymous_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _docx(tmp_path / "r4.docx", ["13800138000", "chen@example.com", "5年经验"])
    node, _ = _node(tmp_path, [json.dumps({"name": None}, ensure_ascii=False)], monkeypatch)

    with pytest.raises(ValidationFailedError) as exc:
        await node(_state("r4"))
    assert exc.value.code == "parse_resume_invalid"
    assert "name" in exc.value.details["missing"]
    assert any("name" in problem for problem in exc.value.details["problems"])


def test_registry_and_image_suffixes_do_not_overlap() -> None:
    """`locate_resume_file` 把两份扩展名表拼在一起，撞了就会找错解析器。"""
    assert not set(default_registry().supported) & set(IMAGE_SUFFIXES)


# --- 回退不重读文件（T-013 / BACKLOG P2-6）---


def _merge(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """把节点返回的增量并进 state，模拟 checkpoint 之后的重放。

    只覆盖主通道：累积通道（`errors` / `trace`）由 LangGraph 的 reducer 追加，
    这里不需要它们参与复用判据。`prompt_version` 是保留键，不进 state。
    """
    return {**base, **{k: v for k, v in update.items() if k != PROMPT_VERSION_KEY}}


async def test_unchanged_file_is_not_read_again_on_the_fallback_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """文本简历：同一份文件连跑两次，第二次不读文件、不打模型〔T-013〕。

    这是 `validate → parse` 回退的既有形状——原件不会变，重读与重问都是白花。
    """
    _docx(tmp_path / "r6.docx", _FULL_TEXT)
    registry = _CountingRegistry([DocxParser()])
    node, fake = _node(tmp_path, [], monkeypatch, registry=registry)

    first = await node(_state("r6"))
    assert (registry.reads, fake.new_count(0)) == (1, 0), "首轮只该读文件，正则全命中不该问模型"

    calls_after_first = len(fake.calls)
    second = await node(_merge(_state("r6"), first))

    assert registry.reads == 1, "复用判据没生效：文件被读了第二遍"
    assert fake.new_count(calls_after_first) == 0, "第二次一次模型都不许打"
    assert second["raw_text"] == first["raw_text"]
    assert second["parsed"] == first["parsed"], "复用必须等价于重跑，不能只快不对"
    assert second["source_stamp"] == first["source_stamp"]
    # 复用轮只多一条批注，其余抽取流水账必须与首轮逐字相同
    assert second["errors"] == [REUSED_SOURCE_NOTE, *first["errors"]]
    assert REUSED_SOURCE_NOTE not in first["errors"], "正常首轮不存在上一轮"


async def test_a_changed_file_restarts_the_whole_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mtime 或大小变了 → 照常重读重抽：复用判据不许把换掉的原件也吞下来。"""
    target = _docx(tmp_path / "r7.docx", _FULL_TEXT)
    registry = _CountingRegistry([DocxParser()])
    node, _ = _node(tmp_path, [], monkeypatch, registry=registry)

    first = await node(_state("r7"))
    _docx(target, ["姓名：陈昊 8年经验", "13800138000 chen@example.com", "技能 Java 精通"])
    second = await node(_merge(_state("r7"), first))

    assert registry.reads == 2, "文件换了还复用，等于拿别人的旧正文出卷子"
    assert second["source_stamp"] != first["source_stamp"]
    assert second["parsed"]["years_experience"] == 8.0
    assert not any("复用" in note for note in second["errors"])


async def test_scanned_pages_are_not_ocr_ed_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """扫描件回退不重跑 OCR——这是本条的成本大头：一次 OCR = 每页一趟视觉往返。"""
    _scanned_pdf(tmp_path / "r8.pdf")
    node, fake = _node(
        tmp_path, [_OCR_PAGE], monkeypatch, vision_model="fake-vision-model"
    )

    first = await node(_state("r8"))
    assert len(fake.calls) == 1, "首轮只有一次 OCR（OCR 文本正则全命中，没二次兜底）"

    calls_after_first = len(fake.calls)
    second = await node(_merge(_state("r8"), first))

    assert fake.new_count(calls_after_first) == 0, "第二次一张图都不许再送进视觉模型"
    assert second["raw_text"] == _OCR_PAGE
    assert second["parsed"]["name"] == "陈昊"
    # 复用支的正文署名从 state 里带回，所以两轮的 trace 署名一样（否则"哪版模板读的图"会丢）
    assert (first[PROMPT_VERSION_KEY], second[PROMPT_VERSION_KEY]) == (stamp("ocr"), stamp("ocr"))


async def test_reuse_needs_a_non_empty_previous_raw_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """state 里有指纹但正文是空串（上一轮什么都没读出来）→ 不复用，照常重读。"""
    _docx(tmp_path / "r9.docx", _FULL_TEXT)
    registry = _CountingRegistry([DocxParser()])
    node, _ = _node(tmp_path, [], monkeypatch, registry=registry)
    target = tmp_path / "r9.docx"
    stale = {
        "resume_id": "r9",
        "raw_text": "",
        "source_stamp": f"{target.stat().st_mtime_ns}:{target.stat().st_size}",
        "source_parser": "DocxParser",
        "source_prompt_version": "",
    }

    update = await node(stale)

    assert registry.reads == 1
    assert update["raw_text"]
    assert not any("复用" in note for note in update["errors"])


async def test_reuse_skips_the_read_but_not_the_ner_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """诚实边界：复用只免掉"读文件"这一趟。姓名没有`姓名：`标签时 NER 兜底照样问一次。

    这条不是漏网的开销，是刻意留的：`validate → parse` 回退的存在理由就是"重问一次抽取"，
    把上一轮的 `parsed` 原样搬回来等于让回退原地打转。T-013 砍掉的是读取与 OCR。
    """
    _docx(tmp_path / "r10.docx", ["陈昊 3年经验", "13800138000 chen@example.com"])
    reply = json.dumps({"name": {"value": "陈昊", "evidence": "陈昊 3年经验"}}, ensure_ascii=False)
    registry = _CountingRegistry([DocxParser()])
    node, fake = _node(tmp_path, [reply], monkeypatch, registry=registry)

    first = await node(_state("r10"))
    assert (registry.reads, len(fake.calls)) == (1, 1), "没标签的姓名要问一次模型"

    second = await node(_merge(_state("r10"), first))

    assert registry.reads == 1, "第二轮不许再读文件"
    assert len(fake.calls) == 2, "第二轮只剩 NER 兜底这一次，OCR/读取都为 0"
    assert second["parsed"]["name"] == "陈昊"
