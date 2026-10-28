"""解析层测试：PdfParser / DocxParser / ImageParser 的失败分支 + Registry 路由与文档流顺序。

真实简历（`tests/fixtures/resumes/`）存在就跑，不存在就跳过——CJK 解码与阅读顺序只有
真文件能证明，合成 PDF 证明不了字体子集化后的解码结果。
"""

import asyncio
import base64
import re
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pymupdf
import pytest

from app.config import get_settings
from app.exceptions.base import ParseError
from app.parsers.base import BaseParser, ParsedDocument, ParserRegistry
from app.parsers.docx_parser import CELL_SEPARATOR, DocxParser
from app.parsers.image_parser import ImageParser
from app.parsers.pdf_parser import PdfParser
from app.utils.llm_client import LLMClient
from tests.test_agents._fake_llm import FakeCompletions, completion, stamp

REAL_RESUME = Path(__file__).parent / "fixtures" / "resumes" / "附件简历-梁清生.pdf"


def _text_pdf(path: Path, lines: list[str]) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    for i, line in enumerate(lines):
        page.insert_text((60, 100 + i * 20), line, fontsize=11)
    doc.save(path)
    doc.close()
    return path


def _paged_text_pdf(path: Path, pages: int, *, marker: str = "Guo Wang Java Redis Kafka") -> Path:
    """每页都有文字层的长 PDF——页数上限的靶子。"""
    doc = pymupdf.open()
    for page_no in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 100), f"{marker} 第 {page_no + 1} 页", fontsize=11)
    doc.save(path)
    doc.close()
    return path


def _scanned_pdf(path: Path, pages: int = 1) -> Path:
    """只有铺满页面的图、没有文字层——真扫描件的形状。"""
    doc = pymupdf.open()
    rect = pymupdf.Rect(0, 0, 595, 842)
    for _ in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_image(rect, pixmap=pymupdf.Pixmap(pymupdf.csGRAY, rect))
    doc.save(path)
    doc.close()
    return path


def _encrypted_pdf(path: Path) -> Path:
    plain = path.with_name("plain_for_encrypt.pdf")
    _text_pdf(plain, ["Chen Hao", "Backend Engineer"])
    doc = pymupdf.open(plain)
    doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")
    doc.close()
    return path


def test_real_resume_decodes_cjk_in_reading_order() -> None:
    if not REAL_RESUME.is_file():
        pytest.skip("缺少真实简历样本")

    document = PdfParser().parse(REAL_RESUME)

    assert document.page_count == 2
    assert not document.is_scanned
    assert "梁清生" in document.raw_text
    assert "17626460250" in document.raw_text and "3405628625@qq.com" in document.raw_text
    # sort=True 的效果：页眉姓名排在正文之前
    assert document.raw_text.index("梁清生") < document.raw_text.index("广州应用科技学院")


def test_text_pdf_roundtrip(tmp_path: Path) -> None:
    pdf = _text_pdf(tmp_path / "text.pdf", ["Guo Wang", "Java Redis Kafka", "Senior Engineer"])
    document = PdfParser().parse(pdf)
    assert "Java Redis Kafka" in document.raw_text
    assert document.page_count == 1


def test_scanned_pdf_is_handed_to_image_path(tmp_path: Path) -> None:
    with pytest.raises(ParseError) as exc:
        PdfParser().parse(_scanned_pdf(tmp_path / "scan.pdf"))
    assert exc.value.code == "parse_scanned_image"
    assert exc.value.status_code == 422


def test_encrypted_pdf_is_not_silently_empty(tmp_path: Path) -> None:
    with pytest.raises(ParseError) as exc:
        PdfParser().parse(_encrypted_pdf(tmp_path / "enc.pdf"))
    assert exc.value.code == "parse_encrypted"


def test_corrupt_pdf_reports_bad_pdf(tmp_path: Path) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 header but no body or xref")
    with pytest.raises(ParseError) as exc:
        PdfParser().parse(broken)
    assert exc.value.code == "parse_bad_pdf"


def _tighten_pdf_limits(monkeypatch: pytest.MonkeyPatch, **values: int) -> None:
    """把 `parse:` 段的上限改小。上限拦的是异常输入，测试不该为此造一份 6 万字的 PDF。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "parse", settings.parse.model_copy(update=values))


def test_a_pdf_over_the_page_cap_is_refused_before_any_page_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """页数闸在 `load_page` **之前**：读完再拒等于把代价付一遍才告诉用户不行〔T-208〕。

    这条断的是调用次序而不是错误码本身——码对了但位置错了，300 页的文档仍然会先把
    进程按在 CPU 上，而那正是本卡要省的东西。
    计数只认**非负**页号：PyMuPDF 自己在 `save`/`close` 里会拿 -1 碰 trailer。
    """
    pdf = _paged_text_pdf(tmp_path / "long.pdf", 3)
    _tighten_pdf_limits(monkeypatch, max_pdf_pages=2)
    loads: list[int] = []
    real_load = pymupdf.Document.load_page

    def _spy(self: pymupdf.Document, index: int) -> Any:
        loads.append(index)
        return real_load(self, index)

    monkeypatch.setattr(pymupdf.Document, "load_page", _spy)

    with pytest.raises(ParseError) as exc:
        PdfParser().parse(pdf)

    assert exc.value.code == "parse_too_many_pages"
    assert exc.value.details["page_count"] == 3
    assert [index for index in loads if index >= 0] == [], f"逐页读取已经跑了 {loads}"


def test_a_pdf_over_the_char_cap_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """字数闸拦的是 prompt 体积：`ner.user.j2` / `validate.user.j2` 裸贴完整 `raw_text`。

    样本每页 44 字，稳过"平均每页 < 40 字 = 无文字层"那道判定，所以报的必须是长度
    而不是扫描件——两条闸各管各的事。
    """
    pdf = _text_pdf(tmp_path / "t.pdf", ["Guo Wang Java Redis Kafka Backend Senior Engineer"])
    _tighten_pdf_limits(monkeypatch, max_raw_chars=30)

    with pytest.raises(ParseError) as exc:
        PdfParser().parse(pdf)

    assert exc.value.code == "parse_too_many_chars"
    assert exc.value.details["max_raw_chars"] == 30


def test_a_scanned_pdf_that_is_too_long_hears_about_length_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超页数的扫描件先报"页数超限"而不是"请改用图片解析"，这是有意的〔T-208〕。

    不改页面内容就没法知道它是不是扫描件，而"读一遍才知道"正是本卡要省掉的开销。
    何况图片路径自己也有 20 页上限（`image_parser._MAX_PAGES`），那句指引对一个
    300 页的文件本来就不成立。
    """
    pdf = _scanned_pdf(tmp_path / "scan.pdf", pages=3)
    _tighten_pdf_limits(monkeypatch, max_pdf_pages=2)

    with pytest.raises(ParseError) as exc:
        PdfParser().parse(pdf)

    assert exc.value.code == "parse_too_many_pages"


def test_default_limits_keep_a_real_resume_far_from_the_cap() -> None:
    """默认上限不许顺手把正常简历挡在门外：真样本实测 2 页 / 2,817 字（2026-10-28）。

    断的是余量倍数而不是绝对值——把 `parse:` 段调小到"测试全绿但真简历进不来"
    是这类配置最容易犯的错。
    """
    if not REAL_RESUME.is_file():
        pytest.skip("缺少真实简历样本")
    limits = get_settings().parse
    document = PdfParser().parse(REAL_RESUME)

    assert document.page_count * 5 <= limits.max_pdf_pages
    assert len(document.raw_text) * 5 <= limits.max_raw_chars


class _FakeParser(BaseParser):
    extensions = (".FAKE",)

    def parse(self, path: Path) -> ParsedDocument:
        return ParsedDocument(raw_text="假内容", page_count=1)


def test_registry_routes_by_extension_case_insensitively(tmp_path: Path) -> None:
    registry = ParserRegistry([PdfParser(), _FakeParser()])
    upload = tmp_path / "whatever.FaKe"
    upload.write_bytes(b"fake resume body")
    document = registry.parse(upload)
    assert document.raw_text == "假内容"
    # registry 负责标注是谁解析的，节点不必自己拼
    assert document.parser == "_FakeParser"


def test_registry_rejects_unsupported_and_missing() -> None:
    registry = ParserRegistry([PdfParser()])

    with pytest.raises(ParseError) as exc:
        registry.parse(Path("resume.txt"))
    assert exc.value.code == "parse_unsupported_type"
    assert ".pdf" in exc.value.details["supported"]

    with pytest.raises(ParseError) as missing:
        registry.parse(Path("no_such_file.pdf"))
    assert missing.value.code == "parse_file_missing"


class _NoExt(BaseParser):
    def parse(self, path: Path) -> ParsedDocument:  # pragma: no cover - 只用于注册失败
        return ParsedDocument(raw_text="", page_count=0)


def test_registry_refuses_broken_registration() -> None:
    with pytest.raises(ValueError, match="未声明 extensions"):
        ParserRegistry([_NoExt()])

    with pytest.raises(ValueError, match="重复注册"):
        ParserRegistry([PdfParser(), PdfParser()])


def _resume_docx(path: Path) -> Path:
    """造一份"页眉放联系方式 + 正文夹表格"的简历，正是真简历最常见的形状。"""
    from docx import Document
    document = Document()
    document.sections[0].header.paragraphs[0].text = "梁清生 17626460250"
    document.add_paragraph("教育经历")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "学校"
    table.cell(0, 1).text = "广州应用科技学院"
    table.cell(1, 0).text = "专业"
    table.cell(1, 1).text = "软件工程"
    document.add_paragraph("技能栈")
    document.save(path)
    return path


def test_docx_keeps_body_stream_order(tmp_path: Path) -> None:
    document = DocxParser().parse(_resume_docx(tmp_path / "resume.docx"))
    text = document.raw_text

    assert text.startswith("梁清生 17626460250")
    assert text.index("教育经历") < text.index("广州应用科技学院") < text.index("技能栈")
    assert f"学校{CELL_SEPARATOR}广州应用科技学院" in text
    # 直接用 parser 实例时 registry 不参与，parser 字段留空
    assert document.parser == ""
    assert document.page_count >= 1


def test_registry_routes_docx_and_marks_parser(tmp_path: Path) -> None:
    registry = ParserRegistry([PdfParser(), DocxParser()])
    document = registry.parse(_resume_docx(tmp_path / "r.docx"))
    assert document.parser == "DocxParser"
    assert ".docx" in registry.supported


def test_doc_rejected_as_unsupported(tmp_path: Path) -> None:
    legacy = tmp_path / "resume.doc"
    legacy.write_bytes(b"\xd0\xcf\x11\xe0 legacy OLE header")
    with pytest.raises(ParseError) as exc:
        ParserRegistry([DocxParser()]).parse(legacy)
    assert exc.value.code == "parse_unsupported_type"


def test_corrupt_docx_reports_bad_docx(tmp_path: Path) -> None:
    fake = tmp_path / "fake.docx"
    fake.write_bytes(b"not a zip at all")
    with pytest.raises(ParseError) as exc:
        DocxParser().parse(fake)
    assert exc.value.code == "parse_bad_docx"


def test_blank_docx_is_not_silently_empty(tmp_path: Path) -> None:
    from docx import Document

    blank = tmp_path / "blank.docx"
    Document().save(blank)
    with pytest.raises(ParseError) as exc:
        DocxParser().parse(blank)
    assert exc.value.code == "parse_empty_docx"


_VISION_MODEL = "fake-vision-model"
_OCR_PAGE = "梁清生 17626460250 广州应用科技学院 软件工程专业 2016 至 2020"
#: 多页用例的正文模板。**必须长过 `_MIN_OCR_CHARS`**：T-104 之后字数不够的页会被当成糊页丢掉，
#: 短回复会让"页序"与"并发"这两条判据测到别的东西上去。


def _page_text(page: object, suffix: str = "页的正文内容足够长，不会被当成糊页丢掉") -> str:
    return f"第{page}{suffix}"


def _vision_parser(
    monkeypatch: pytest.MonkeyPatch, replies: Sequence[str]
) -> tuple[ImageParser, FakeCompletions]:
    """按页序吐回复的视觉客户端。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_model_vision", _VISION_MODEL)
    fake = FakeCompletions(replies)
    return ImageParser(llm=LLMClient(settings=settings, client=fake)), fake


def _user_blocks(fake: FakeCompletions, index: int = 0) -> list[dict[str, Any]]:
    """取那次请求 user 消息的正文内容块列表。"""
    return fake.calls[index]["messages"][-1]["content"]


def _decoded_image(block: dict[str, Any]) -> pymupdf.Pixmap:
    header, _, payload = block["image_url"]["url"].partition(",")
    assert header == "data:image/jpeg;base64", "统一转 JPEG，透明 PNG 会让模型收到乱码"
    return pymupdf.Pixmap(base64.b64decode(payload))


async def test_vision_png_upload_is_scanned_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = tmp_path / "resume.png"
    pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 800, 1000)).save(shot)
    parser, fake = _vision_parser(monkeypatch, [_OCR_PAGE])

    document = await parser.parse(shot)

    assert document.is_scanned is True
    assert document.page_count == 1
    assert document.raw_text == _OCR_PAGE
    assert document.parser == "ImageParser", "不走 registry，得自己署名"
    assert document.prompt_version == stamp("ocr")
    assert fake.calls[0]["model"] == _VISION_MODEL, "必须走 vision 路由，不能落到文本模型"
    assert fake.calls[0]["temperature"] == 0.0, "抄写任务不该有创造性"


async def test_vision_request_body_is_content_blocks(tmp_path: Path, monkeypatch) -> None:
    shot = tmp_path / "resume.jpg"
    pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, 600, 800)).save(shot)
    parser, fake = _vision_parser(monkeypatch, [_OCR_PAGE])

    await parser.parse(shot)

    messages = fake.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert isinstance(messages[0]["content"], str)
    blocks = _user_blocks(fake)
    assert blocks[0]["type"] == "text" and "第 1/1 页" in blocks[0]["text"]
    assert blocks[1]["type"] == "image_url"


async def test_vision_reads_every_pdf_page_in_order(tmp_path: Path, monkeypatch) -> None:
    first = _page_text(1)
    second = _page_text(2)
    parser, fake = _vision_parser(monkeypatch, [first, second])

    document = await parser.parse(_scanned_pdf(tmp_path / "scan.pdf", pages=2))

    assert document.page_count == 2
    assert len(fake.calls) == 2
    assert "第 2/2 页" in _user_blocks(fake, 1)[0]["text"]
    assert document.raw_text == f"{first}\n\n{second}"


class _BackwardVision:
    """越靠后的页回得越快，且按**请求里标明的页号**给文字。

    这是 `gather` 唯一的证伪办法：完成顺序与提交顺序相反时，若拼装靠的是完成顺序，
    文本就会倒过来。（按"第 N/M 页"取页号，是因为正文里的页码就是请求带的页号。）
    """

    def __init__(self, total: int) -> None:
        self.total = total
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs: Any) -> Any:
        blocks = kwargs["messages"][-1]["content"]
        page = int(re.search(r"第 (\d+)/", str(blocks[0]["text"])).group(1))
        await asyncio.sleep((self.total - page) * 0.02)
        self.calls.append(kwargs)
        return completion(_page_text(page))


async def test_concurrent_pages_keep_page_order(tmp_path: Path, monkeypatch) -> None:
    """多页 OCR 并发，但页序仍按页号排〔T-014 / BACKLOG P2-4〕。"""
    settings = get_settings().model_copy(deep=True)
    monkeypatch.setattr(settings, "llm_model_vision", _VISION_MODEL)
    # 三次往返必须同时在飞，所以并发上限由用例自己给（与 `..._not_one_after_another` 同一条理由）：
    # 拿环境配置就等于让"页序拼装"这条断言跟着 `llm.max_concurrency` 漂，
    # 而 testing.yaml 的 2 会让第三页排到队首之后才发出，完成顺序不再是 3→2→1。
    monkeypatch.setattr(settings.llm, "max_concurrency", 3)
    fake = _BackwardVision(total=3)
    parser = ImageParser(llm=LLMClient(settings=settings, client=fake))

    document = await parser.parse(_scanned_pdf(tmp_path / "scan.pdf", pages=3))

    assert len(fake.calls) == 3, "三页就该三次往返"
    # 完成顺序是 3 → 2 → 1（越靠后的页回得越快）
    done_order = [
        int(re.search(r"第 (\d+)/", str(c["messages"][-1]["content"][0]["text"])).group(1))
        for c in fake.calls
    ]
    assert done_order == [3, 2, 1]
    assert document.raw_text == "\n\n".join(_page_text(page) for page in (1, 2, 3))
    assert document.page_count == 3


async def test_rasterization_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """栅格化整段进 worker 线程：事件循环不该被 `pymupdf` 占住〔T-014〕。

    判据打在"调用形状"上而不是计时上（sleep 测不出毫秒级的循环占用）。
    """
    parser, _ = _vision_parser(monkeypatch, [_OCR_PAGE])
    shot = tmp_path / "resume.png"
    pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 500)).save(shot)

    called: list[Any] = []
    original_to_thread = asyncio.to_thread

    async def spy(func: Any, *args: Any, **kwargs: Any) -> Any:
        called.append((getattr(func, "__name__", repr(func)), args))
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy)

    await parser.parse(shot)

    # 线程里跑的是 `_to_data_urls`，参数是路径本身（不是绑定的 self）——
    # 绑定的 self 会把整台 parser（连同信号量与客户端）交给 worker 线程
    assert ("_to_data_urls", (shot,)) in called


async def test_pages_reach_the_model_together_not_one_after_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """三次往返同时在飞：串行时这里会卡在 barrier 上超时〔T-014〕。

    限流仍然只有 `LLMClient` 里那一把 `Semaphore`——本用例不数并发上限，只证明
    解析器自己没有再排一队。
    """
    settings = get_settings().model_copy(deep=True)
    monkeypatch.setattr(settings, "llm_model_vision", _VISION_MODEL)
    # 信号量按环境配置可能是 2（`configs/testing.yaml`），barrier 会等不齐——用例自己放大到够宽
    monkeypatch.setattr(settings.llm, "max_concurrency", 4)
    barrier = asyncio.Barrier(3)

    class _MeetMe:
        def __init__(self) -> None:
            self.submitted: list[str] = []
            self.chat = SimpleNamespace(completions=self)

        async def create(self, **kwargs: Any) -> Any:
            blocks = kwargs["messages"][-1]["content"]
            page = re.search(r"第 (\d+)/", str(blocks[0]["text"])).group(1)
            self.submitted.append(page)
            await asyncio.wait_for(barrier.wait(), timeout=2)
            return completion(_page_text(page))

    fake = _MeetMe()
    parser = ImageParser(llm=LLMClient(settings=settings, client=fake))

    document = await parser.parse(_scanned_pdf(tmp_path / "scan.pdf", pages=3))

    assert sorted(fake.submitted) == ["1", "2", "3"]
    assert document.raw_text == "\n\n".join(_page_text(page) for page in (1, 2, 3))


async def test_self_built_llm_client_is_closed_but_injected_one_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """自建客户端在 parse 收尾关闭（连失败的运行也关），注入的留给调用方〔T-014 顺带〕。"""
    settings = get_settings().model_copy(deep=True)
    monkeypatch.setattr(settings, "llm_model_vision", _VISION_MODEL)
    shot = tmp_path / "resume.png"
    pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 500)).save(shot)

    class _TrackingClient(LLMClient):
        built: list[_TrackingClient] = []

        def __init__(self, settings: Any = None, client: Any = None) -> None:
            super().__init__(settings, client or object())
            _TrackingClient.built.append(self)

    monkeypatch.setattr("app.parsers.image_parser.LLMClient", _TrackingClient)

    injected = LLMClient(settings=settings, client=FakeCompletions([_OCR_PAGE]))
    await ImageParser(llm=injected).parse(shot)
    assert injected._closed is False, "注入的客户端归调用方管"

    # 底层是一件都没给的空对象：真实 `_to_data_urls` 之后第一次模型往返就在 `.chat` 上炸，
    # 但 `finally` 必须已经把自己建的那个客户端关掉了（不注入真密钥）
    parser = ImageParser(settings=settings)
    with pytest.raises(AttributeError):
        await parser.parse(shot)

    self_built = [client for client in _TrackingClient.built if client is not injected]
    assert len(self_built) == 1 and self_built[0]._closed is True


async def test_vision_downscales_oversized_image(tmp_path: Path, monkeypatch) -> None:
    huge = tmp_path / "huge.png"
    pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 3200, 2400)).save(huge)
    parser, fake = _vision_parser(monkeypatch, [_OCR_PAGE])

    await parser.parse(huge)

    sent = _decoded_image(_user_blocks(fake)[1])
    assert max(sent.width, sent.height) <= 1560


async def test_vision_without_configured_model_refuses_to_guess(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_model_vision", "")

    with pytest.raises(ParseError) as exc:
        ImageParser(settings=settings)
    assert exc.value.code == "parse_vision_unconfigured"
    assert "LLM_MODEL_VISION" in exc.value.message


async def test_vision_that_reads_nothing_is_an_error(tmp_path: Path, monkeypatch) -> None:
    parser, _ = _vision_parser(monkeypatch, ["   ", ""])

    with pytest.raises(ParseError) as exc:
        await parser.parse(_scanned_pdf(tmp_path / "scan.pdf", pages=2))
    assert exc.value.code == "parse_vision_empty"
    assert exc.value.details["pages"] == 2


async def test_vision_blurry_page_is_dropped_instead_of_concatenated(
    tmp_path: Path, monkeypatch
) -> None:
    """字数不够的页**不进正文**，只留一条流水账〔T-104 / TODO §2.6〕。

    实测一张纯灰度图 OCR 回的是模型自己的话"（图片中未出现可识别的文字）"（14 字）。
    以前它原样进了 `raw_text`/`jd_text`，等于让模型的自述冒充海报内容。
    判据只用"字数不够"这一条硬事实——不去认那种措辞，关键词黑名单会变成第二套判据。
    """
    parser, _ = _vision_parser(monkeypatch, ["糊", _OCR_PAGE])

    document = await parser.parse(_scanned_pdf(tmp_path / "scan.pdf", pages=2))

    assert len(document.warnings) == 1
    assert "第 1 页" in document.warnings[0]
    assert document.raw_text == _OCR_PAGE


async def test_vision_meta_answer_alone_is_an_empty_read(tmp_path: Path, monkeypatch) -> None:
    """整份图只换来一句自述 → 按"没读出文字"失败，不产出只含元话的正文。"""
    parser, _ = _vision_parser(monkeypatch, ["（图片中未出现可识别的文字）"])

    with pytest.raises(ParseError) as exc:
        await parser.parse(_scanned_pdf(tmp_path / "scan.pdf", pages=1))
    assert exc.value.code == "parse_vision_empty"


async def test_vision_spaced_latin_letters_are_glued_back(tmp_path: Path, monkeypatch) -> None:
    """`P y t h o n` 在 OCR 层就压回词形，否则技能词典一个都不认〔T-104 / TODO §2.6〕。"""
    spaced = "要求：P y t h o n 与 R e d i s 缓存，C C++ 是两门语言"
    parser, _ = _vision_parser(monkeypatch, [spaced])
    shot = tmp_path / "resume.png"
    pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 500)).save(shot)

    document = await parser.parse(shot)

    assert "Python" in document.raw_text
    assert "Redis" in document.raw_text
    # 保守判据：只有连续 ≥3 个单字母才算逐字失真，两个字母的并列必须原样留着
    assert "C C++" in document.raw_text


async def test_vision_failure_branches(tmp_path: Path, monkeypatch) -> None:
    parser, _ = _vision_parser(monkeypatch, [_OCR_PAGE])

    broken = tmp_path / "broken.png"
    broken.write_bytes(b"\x89PNG\r\n\x1a\n then nothing")
    with pytest.raises(ParseError) as bad_image:
        await parser.parse(broken)
    assert bad_image.value.code == "parse_bad_image"

    with pytest.raises(ParseError) as wrong_type:
        await parser.parse(tmp_path / "resume.doc")
    assert wrong_type.value.code == "parse_unsupported_type"
    assert ".pdf" in wrong_type.value.details["supported"]

    with pytest.raises(ParseError) as too_many:
        await parser.parse(_scanned_pdf(tmp_path / "fat.pdf", pages=25))
    assert too_many.value.code == "parse_too_many_pages"
    assert too_many.value.details["page_count"] == 25

    with pytest.raises(ParseError) as encrypted:
        await parser.parse(_encrypted_pdf(tmp_path / "enc.pdf"))
    assert encrypted.value.code == "parse_encrypted"

