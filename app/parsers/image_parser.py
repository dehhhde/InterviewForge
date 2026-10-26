"""图片简历 / 扫描件解析：多模态 LLM 兜底〔阶段 5 · §8.4，选型见 docs/adr/0006〕

PaddleOCR 在 Python 3.14 无分发，所以扫描件唯一的出路是让多模态模型读图。

它**不在 `ParserRegistry` 里**：`BaseParser.parse` 是同步契约，而这里等的是 LLM 网络往返。
节点在两种情况下直接用 `await ImageParser(...).parse(path)`：
1. 上传文件扩展名是图片；
2. `PdfParser` 抛出 `ParseError(code="parse_scanned_image")`。
"""

import base64
from pathlib import Path

import pymupdf

from app.config import Settings, get_settings
from app.exceptions.base import ParseError
from app.parsers.base import ParsedDocument
from app.prompts.registry import Prompt, get_prompt
from app.utils.llm_client import LLMClient

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff")

#: 视觉模型普遍把输入下采样到 1560px 左右，再大只是白烧 base64 与带宽。
_MAX_SIDE = 1560
_JPG_QUALITY = 85
#: 一页识别结果少于这个字数，基本就是空白页或糊成一团
_MIN_OCR_CHARS = 20
#: 成本护栏：扫描件正常 1-3 页，超过这个数就当异常拒绝，不给"500 页 PDF 打爆配额"开门
_MAX_PAGES = 20


class ImageParser:
    """把图片或扫描 PDF 的每一页交给视觉模型，产出 `is_scanned=True` 的文档。"""

    def __init__(self, llm: LLMClient | None = None, settings: Settings | None = None) -> None:
        self.settings = settings or (llm.settings if llm is not None else get_settings())
        if not self.settings.llm_model_vision:
            # 绝不退回纯文本模型：文本模型"读"图不会报错，只会编出一份看似完整的简历
            raise ParseError(
                "无法解析扫描件：未配置视觉模型，请在 .env 设置 LLM_MODEL_VISION",
                code="parse_vision_unconfigured",
            )
        self._llm = llm or LLMClient(self.settings)

    @property
    def name(self) -> str:
        return type(self).__name__

    async def parse(self, path: Path) -> ParsedDocument:
        images = self._to_data_urls(path)
        prompt = get_prompt("ocr")

        pages: list[str] = []
        warnings: list[str] = []
        for index, data_url in enumerate(images, start=1):
            text = (await self._read_page(prompt, data_url, index, len(images))).strip()
            if len(text) < _MIN_OCR_CHARS:
                warnings.append(f"第 {index} 页只识别出 {len(text)} 字，可能是空白页或图像过糊")
            pages.append(text)

        raw_text = "\n\n".join(text for text in pages if text)
        if not raw_text:
            raise ParseError(
                "视觉模型没有读出任何文字",
                code="parse_vision_empty",
                details={"path": str(path), "pages": len(images)},
            )

        return ParsedDocument(
            raw_text=raw_text,
            page_count=len(images),
            is_scanned=True,
            # 不走 registry，没人替它署名
            parser=self.name,
            prompt_version=f"{prompt.node}@{prompt.version}",
            warnings=tuple(warnings),
        )

    async def _read_page(self, prompt: Prompt, data_url: str, page_no: int, page_total: int) -> str:
        messages = prompt.render(page_no=page_no, page_total=page_total)
        *rest, user = messages
        # 多模态请求的正文必须是内容块列表，不能是字符串。
        content = [
            {"type": "text", "text": str(user["content"])},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]
        return await self._llm.chat(
            "vision", [*rest, {**user, "content": content}], temperature=0.0
        )

    def _to_data_urls(self, path: Path) -> list[str]:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            return self._pdf_data_urls(path)
        if suffix not in IMAGE_SUFFIXES:
            raise ParseError(
                f"图片解析不支持的文件类型：{suffix or '(无扩展名)'}",
                code="parse_unsupported_type",
                details={"file": path.name, "supported": [*IMAGE_SUFFIXES, ".pdf"]},
            )
        try:
            pixmap = pymupdf.Pixmap(path)
        except Exception as exc:  # MuPDF 的图像错误类型不稳定，统一转成 ParseError
            raise ParseError(
                "图片读不出来，可能已损坏或不是图片",
                code="parse_bad_image",
                details={"path": str(path), "reason": str(exc)},
            ) from exc
        return [self._encode(pixmap)]

    def _pdf_data_urls(self, path: Path) -> list[str]:
        try:
            doc = pymupdf.open(path)
        except (pymupdf.FileDataError, pymupdf.FileNotFoundError, pymupdf.EmptyFileError) as exc:
            raise ParseError(
                "扫描件 PDF 打不开",
                code="parse_bad_pdf",
                details={"path": str(path), "reason": str(exc)},
            ) from exc

        with doc:
            if doc.needs_pass:
                raise ParseError(
                    "PDF 已加密，无法读取扫描件",
                    code="parse_encrypted",
                    details={"path": str(path)},
                )
            if doc.page_count == 0:
                raise ParseError(
                    "PDF 没有任何页面", code="parse_empty_pdf", details={"path": str(path)}
                )
            if doc.page_count > _MAX_PAGES:
                raise ParseError(
                    f"扫描件页数 {doc.page_count} 超过单次解析上限 {_MAX_PAGES}",
                    code="parse_too_many_pages",
                    details={"path": str(path), "page_count": doc.page_count},
                )
            urls: list[str] = []
            for index in range(doc.page_count):
                page = doc.load_page(index)
                zoom = _MAX_SIDE / max(page.rect.width, page.rect.height)
                urls.append(self._encode(page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))))
        return urls

    @staticmethod
    def _encode(pixmap: pymupdf.Pixmap) -> str:
        """统一成 JPEG data URL：去透明通道（JPEG 不支持）、转 RGB（CMYK 常见于扫描）、
        逐步减半直到长边达标。"""
        image = pixmap
        if image.alpha:
            image = pymupdf.Pixmap(image, 0)
        if image.colorspace is not None and image.n > 3:
            image = pymupdf.Pixmap(pymupdf.csRGB, image)
        while max(image.width, image.height) > _MAX_SIDE and min(image.width, image.height) > 2:
            image.shrink(1)
        encoded = base64.b64encode(image.tobytes("jpg", _JPG_QUALITY)).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"
