"""简历解析节点：定位文件 → parsers → extractors → 写 raw_text / parsed〔阶段 7 · §10.4.1〕

节点契约（计划书 §4）在这里的落法：
- 只返回增量 `{raw_text, parsed, errors}`，`prompt_version` 走保留键交给 `traced()`；
- 同步解析器（PDF/DOCX 是 CPU 密集）用 `asyncio.to_thread` 调，不阻塞事件循环；
- 图片与扫描件走 async 的 `ImageParser`，所以它不在 `ParserRegistry` 里；
- 不可恢复的失败（找不到文件、抽不出姓名）直接抛 `AppError` 子类，不返回空结果。

文件命名约定：`<upload_dir>/<resume_id><原始扩展名>`。上传接口落地前由本模块
`locate_resume_file()` 认这个形状，届时改成 service 提供路径即可。
"""

import asyncio
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.agents.state import AgentState
from app.agents.workflow import PROMPT_VERSION_KEY, Node
from app.config import Settings, get_settings
from app.exceptions.base import NotFoundError, ParseError, ValidationFailedError
from app.extractors.ner_extractor import NerExtractor, NerResult
from app.extractors.project_extractor import extract_projects
from app.extractors.skill_extractor import extract_skills
from app.extractors.timeline_extractor import TimelineResult, extract_timeline
from app.parsers.base import ParsedDocument, ParserRegistry
from app.parsers.docx_parser import DocxParser
from app.parsers.image_parser import IMAGE_SUFFIXES, ImageParser
from app.parsers.pdf_parser import PdfParser
from app.schemas.resume import ResumeSchema
from app.services.resume_service import YEARS_TOLERANCE
from app.utils.llm_client import LLMClient

#: resume_id 会被拼进文件路径，先挡掉任何穿越意图（`..`、`/`、`\`）
_UNSAFE_ID_RE = re.compile(r"[/\\]|\.\.|^\.")


def default_registry() -> ParserRegistry:
    """同步解析器注册表。图片不在其中——它要等 LLM，是 async 的。"""
    return ParserRegistry([PdfParser(), DocxParser()])


def locate_resume_file(
    upload_dir: Path, resume_id: str, suffixes: Sequence[str]
) -> Path:
    """按命名约定找出简历原件。找不到就 404，不猜。"""
    if not resume_id or _UNSAFE_ID_RE.search(resume_id):
        raise ValidationFailedError(
            "缺少或非法的 resume_id，无法定位简历文件",
            code="parse_bad_resume_id",
            details={"resume_id": resume_id},
        )

    for suffix in suffixes:
        candidate = upload_dir / f"{resume_id}{suffix}"
        if candidate.is_file():
            return candidate
    raise NotFoundError(
        "找不到该简历的原始文件，可能已过期清理",
        code="parse_file_missing",
        details={"resume_id": resume_id, "upload_dir": str(upload_dir), "tried": list(suffixes)},
    )


def make_parse_node(
    *,
    llm: LLMClient | None = None,
    registry: ParserRegistry | None = None,
    settings: Settings | None = None,
    upload_dir: Path | None = None,
) -> Node:
    """造 parse 节点。依赖都在装配期注入，节点内部不再 new client。"""
    settings = settings or (llm.settings if llm is not None else get_settings())
    parser_registry = registry or default_registry()
    llm_client = llm or LLMClient(settings)
    uploads = upload_dir or settings.upload_dir
    suffixes = (*parser_registry.supported, *IMAGE_SUFFIXES)

    async def parse_node(state: AgentState) -> dict[str, Any]:
        path = locate_resume_file(uploads, state.get("resume_id") or "", suffixes)
        document = await _read_document(path, parser_registry, llm_client)
        raw_text = document.raw_text

        ner = await NerExtractor(llm=llm_client, settings=settings).extract(raw_text)
        # 另外三个抽取器不花钱，也不该花钱：栏目、日期区间和技能名都有稳定字形
        timeline = extract_timeline(raw_text)
        skills = extract_skills(raw_text)
        projects = extract_projects(raw_text)

        errors = [f"parse: {warning}" for warning in document.warnings]
        for notes in (ner.notes, timeline.notes, skills.notes, projects.notes):
            errors += [f"parse: {note}" for note in notes]
        # `skills.unmapped` 不进 errors：那是"该补词典"的开发提示，不是这份简历的缺陷

        fields: dict[str, Any] = {}
        fields.update(ner.as_resume_fields())
        fields.update(timeline.as_resume_fields())
        fields.update(skills.as_resume_fields())
        fields.update(projects.as_resume_fields())
        errors += _years_reconciliation(ner, timeline)
        if "years_experience" not in fields:
            # 必填字段没着落时按 0 占位并记一条——让图继续走，由校验层判这份简历的成色
            fields["years_experience"] = 0.0
            errors.append("parse: 既没抽到「N 年经验」字样，也没认出任何日期区间，年限按 0 占位")

        return {
            "raw_text": raw_text,
            "parsed": _validate(fields, document, ner),
            "errors": errors,
            PROMPT_VERSION_KEY: _prompt_stamp(document, ner.prompt_version),
        }

    return parse_node


def _years_reconciliation(ner: NerResult, timeline: TimelineResult) -> list[str]:
    """自称的年限与时间线算出的年限对不上时留话，判罚交给 M2 校验层。

    `parsed.years_experience` 取时间线那个值——它是从日期区间复算的，出题时按它设难度
    才站得住；自称的值只当线索，不覆盖。
    """
    claimed, computed = ner.years_experience, timeline.years_experience
    if claimed is None or computed is None:
        return []
    gap = abs(float(claimed.value) - float(computed.value))
    if gap <= YEARS_TOLERANCE:
        return []
    return [
        f"parse: 简历自称 {claimed.value} 年经验，按日期区间算是 {computed.value} 年，"
        f"差了 {round(gap, 1)} 年，取时间线的值，请校验层判是否自洽"
    ]


async def _read_document(
    path: Path, registry: ParserRegistry, llm: LLMClient
) -> ParsedDocument:
    """图片直接交给视觉路径；PDF 报"这是扫描件"时也转过去——那是扫描件唯一出路。"""
    if path.suffix.lower() in IMAGE_SUFFIXES:
        return await ImageParser(llm=llm).parse(path)
    try:
        return await asyncio.to_thread(registry.parse, path)
    except ParseError as exc:
        if exc.code != "parse_scanned_image":
            raise
        return await ImageParser(llm=llm).parse(path)


def _validate(fields: dict[str, Any], document: ParsedDocument, ner: NerResult) -> dict[str, Any]:
    """抽到的字段过 `ResumeSchema`，保证 `parsed` 通道的形状。"""
    try:
        return ResumeSchema.model_validate(fields).model_dump(mode="json")
    except ValidationError as exc:
        raise ValidationFailedError(
            "简历抽取结果不合格，无法进入校验环节",
            code="parse_resume_invalid",
            details={
                "parser": document.parser,
                "missing": list(ner.missing),
                "problems": [
                    f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                    for e in exc.errors(include_url=False)
                ],
            },
        ) from exc


def _prompt_stamp(document: ParsedDocument, ner_version: str) -> str:
    """本次真用过的模板，形如 `ner@v1+ocr@v1`；没调 LLM 时是空串。"""
    used = sorted({v for v in (document.prompt_version, ner_version) if v})
    return "+".join(used)
