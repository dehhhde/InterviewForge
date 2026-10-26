"""parse 节点的接线测试：文件定位 → 解析 → 抽取 → `parsed` 通道形状（全程离线）。

断言的是通道路由、失败码与幂等性，不断言模型文案；LLM 回复一律预置。
"""

import json
import shutil
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pymupdf
import pytest

from app.agents.nodes.parse_node import default_registry, locate_resume_file, make_parse_node
from app.agents.state import AgentState
from app.agents.workflow import PROMPT_VERSION_KEY
from app.config import get_settings
from app.exceptions.base import NotFoundError, ValidationFailedError
from app.parsers.image_parser import IMAGE_SUFFIXES
from app.utils.llm_client import LLMClient

REAL_RESUME = Path(__file__).parent.parent / "fixtures" / "resumes" / "附件简历-梁清生.pdf"

_NAME_REPLY = json.dumps(
    {"name": {"value": "梁清生", "evidence": "梁清生"}}, ensure_ascii=False
)
_OCR_PAGE = "姓名：陈昊 13800138000 chen@example.com 3年工作经验"


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


def _node(
    tmp_path: Path,
    replies: Sequence[str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    vision_model: str | None = None,
) -> tuple[Any, _FakeCompletions]:
    settings = get_settings()
    # settings 是 lru_cache 的单例，必须用 monkeypatch 改，直接赋值会漏给后面的测试
    monkeypatch.setattr(settings, "llm_model_vision", vision_model or "")
    fake = _FakeCompletions(replies)
    node = make_parse_node(
        llm=LLMClient(settings=settings, client=fake),
        settings=settings,
        upload_dir=tmp_path,
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
    assert update[PROMPT_VERSION_KEY] == "ner@v1"
    assert len(fake.calls) == 1, "正则白拿的字段不该再问模型"


async def test_parse_node_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not REAL_RESUME.is_file():
        pytest.skip("缺少真实简历样本")
    _mount(tmp_path, "r1", REAL_RESUME)
    node, _ = _node(tmp_path, [_NAME_REPLY], monkeypatch)

    assert await node(_state("r1")) == await node(_state("r1"))


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
    assert update[PROMPT_VERSION_KEY] == "ner@v1"


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
    assert update[PROMPT_VERSION_KEY] == "ocr@v1"


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
