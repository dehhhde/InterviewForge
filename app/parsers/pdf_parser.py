"""PDF 解析 PyMuPDF：坐标还原 + 扫描件判定 + 读取上限〔阶段 5 · §8.2，选型见 docs/adr/0006〕"""

from pathlib import Path

import pymupdf

from app.config import get_settings
from app.exceptions.base import ParseError
from app.parsers.base import BaseParser, ParsedDocument

#: 平均每页文字少于此字数就认为没有文字层。不能只看图片数——真简历普遍带头像和图标。
_MIN_CHARS_PER_PAGE = 40


class PdfParser(BaseParser):
    """整份扫描件直接抛错，交给 `image_parser`（多模态 LLM）；不静默返回空文本。"""

    extensions = (".pdf",)

    def parse(self, path: Path) -> ParsedDocument:
        try:
            doc = pymupdf.open(path)
        except (pymupdf.FileDataError, pymupdf.FileNotFoundError, pymupdf.EmptyFileError) as exc:
            raise ParseError(
                "PDF 打不开，可能已损坏或不是 PDF",
                code="parse_bad_pdf",
                details={"path": str(path), "reason": str(exc)},
            ) from exc

        with doc:
            if doc.needs_pass:
                raise ParseError(
                    "PDF 已加密，无法读取正文",
                    code="parse_encrypted",
                    details={"path": str(path)},
                )
            page_count = doc.page_count
            # 上限拦在逐页读取**之前**：`load_page` 是这份解析里唯一随页数线性增长的开销，
            # 300 页的文档读完再拒等于把代价全付一遍再告诉用户不行
            limits = get_settings().parse
            if page_count > limits.max_pdf_pages:
                raise ParseError(
                    f"PDF 共 {page_count} 页，超过单次解析上限 {limits.max_pdf_pages} 页",
                    code="parse_too_many_pages",
                    details={"page_count": page_count, "max_pdf_pages": limits.max_pdf_pages},
                )
            # sort=True：MuPDF 默认按内容流顺序输出，实测真简历里姓名块排在教育块之后，
            # 不按坐标排就会把版式信息喂错给抽取器
            pages = [doc.load_page(i).get_text("text", sort=True) for i in range(doc.page_count)]

        if page_count == 0:
            raise ParseError(
                "PDF 没有任何页面", code="parse_empty_pdf", details={"path": str(path)}
            )

        # 平均字数低于阈值的页视为"无文字层页"，全表如此则整份是扫描件
        thin_pages = [
            i for i, text in enumerate(pages, start=1) if len(text.strip()) < _MIN_CHARS_PER_PAGE
        ]
        if len(thin_pages) == page_count:
            raise ParseError(
                f"PDF 判定为扫描件（{page_count} 页均无文字层），请改用图片解析",
                code="parse_scanned_image",
                details={"path": str(path), "page_count": page_count},
            )

        raw_text = "\n\n".join(text.strip() for text in pages if text.strip())
        # 字数闸只管长度不管版式：它拦的是 prompt 体积（`ner.user.j2` / `validate.user.j2`
        # 裸贴完整 `raw_text`，每轮 validate 回退再贴一遍），所以放在"读完了"的这一侧
        if len(raw_text) > limits.max_raw_chars:
            raise ParseError(
                f"文字层共 {len(raw_text)} 字，超过单次解析上限 {limits.max_raw_chars} 字",
                code="parse_too_many_chars",
                details={"chars": len(raw_text), "max_raw_chars": limits.max_raw_chars},
            )
        warnings = tuple(
            f"第 {page_no} 页文字层过薄（{len(pages[page_no - 1].strip())} 字），该页内容可能缺失"
            for page_no in thin_pages
        )
        return ParsedDocument(
            raw_text=raw_text,
            page_count=page_count,
            is_scanned=False,
            warnings=warnings,
        )
