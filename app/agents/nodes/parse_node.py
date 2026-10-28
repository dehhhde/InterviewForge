"""简历解析节点：定位文件 → parsers → extractors → 写 raw_text / parsed〔阶段 7 · §10.4.1〕

节点契约（计划书 §4）在这里的落法：
- 只返回增量 `{raw_text, parsed, errors}`，`prompt_version` 走保留键交给 `traced()`；
- 同步解析器（PDF/DOCX 是 CPU 密集）用 `asyncio.to_thread` 调，不阻塞事件循环；
- 图片与扫描件走 async 的 `ImageParser`，所以它不在 `ParserRegistry` 里；
- **回退不重读文件**：`validate → parse` 会带着上一轮的 `raw_text` 重进这里，文件指纹
  （mtime+size）没变就跳过读取与 OCR，只做正则抽取 + 一次 NER 兜底（T-013）；
- 不可恢复的失败（找不到文件、抽不出姓名）直接抛 `AppError` 子类，不返回空结果。

文件命名约定：`<upload_dir>/<resume_id><原始扩展名>`。上传接口落地前由本模块
`locate_resume_file()` 认这个形状，届时改成 service 提供路径即可。
"""

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.agents.state import AgentState
from app.agents.workflow import PROMPT_VERSION_KEY, Node
from app.config import Settings, get_settings
from app.exceptions.base import NotFoundError, ParseError, ValidationFailedError
from app.extractors.ner_extractor import NerExtractor, NerResult
from app.extractors.project_extractor import extract_projects
from app.extractors.sections import split_sections
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


def _file_stamp(path: Path) -> str:
    """文件的"没变过"指纹：`修改时间:字节数`。

    不用内容 hash：判据要的是"回退那一圈里原件没被人换过"，`stat()` 两条就够，
    而 hash 要把整份 PDF 再读一遍——那正是本条要避免的开销（T-013 / BACKLOG P2-6）。
    """
    info = path.stat()
    return f"{info.st_mtime_ns}:{info.st_size}"


def _reusable(state: AgentState, path: Path) -> bool:
    """上一轮已经读出过这份**同一个**文件，且读出来的正文还在 state 里。

    两条都得成立才复用：正文在但文件换了，等于拿别人的简历出卷子；
    文件没换但正文是空的（上一次什么都没读出来），复用只会让回退白跑一圈。
    """
    stamp = state.get("source_stamp")
    raw_text = state.get("raw_text")
    if not stamp or not raw_text:
        return False
    try:
        return _file_stamp(path) == stamp
    except OSError:
        # stat 都失败了（文件正被删），按"不能复用"处理：让下面的读取自己报它该报的错
        return False


#: 复用上一轮正文时写进 `errors` 的那一条。测试与排障都按这个前缀认"这一圈没重读文件"，
#: 所以它留在模块顶层而不是内联在节点里。
REUSED_SOURCE_NOTE = "parse: 文件未变动，复用了上一轮读出的正文（没有重读文件，也没有重跑 OCR）"


@dataclass(frozen=True)
class ReadOutcome:
    """一次"把文件读成正文"的结果。

    只留 parse 需要的四样，`ParsedDocument` 本身不进 state：回退复用时拿不到它，
    两套形状会让"正常跑"与"复用跑"产出不同的 trace 署名。
    """

    raw_text: str
    parser: str
    #: 正文自带的模板署名（`ocr@v1`；文本解析器不走 prompt，是空串）
    prompt_version: str
    warnings: tuple[str, ...] = ()
    #: 正文来自视觉模型（图片或扫描件 PDF）。复用这一支时**不许**再把它送进 OCR
    from_ocr: bool = False


def _reusable_source(state: AgentState, path: Path) -> ReadOutcome | None:
    """回退重跑时，state 里那份正文还对应**同一个**文件就照用，一次模型往返都不花。"""
    if not _reusable(state, path):
        return None
    version = str(state.get("source_prompt_version") or "")
    return ReadOutcome(
        raw_text=str(state.get("raw_text") or ""),
        parser=str(state.get("source_parser") or ""),
        prompt_version=version,
        # 扫描件（`ocr@v1`）复用这一支时绝不重新 OCR：重跑一次就是整份视觉往返
        from_ocr=version.startswith("ocr@"),
    )


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
        # validate → parse 回退会带着上一轮的正文重进这里。文件没换就别再读第二遍、
        # 更别再 OCR 第二遍；正文是 OCR 出来的那几行，靠 `source_prompt_version` 认（T-013）
        reused = _reusable_source(state, path)
        document = reused or await _read_document(path, parser_registry, llm_client)
        raw_text = document.raw_text

        ner = await NerExtractor(llm=llm_client, settings=settings).extract(raw_text)
        # 另外三个抽取器不花钱，也不该花钱：栏目、日期区间和技能名都有稳定字形。
        # 栏目表切一次给三家共用（T-208）：它原先在每个抽取器里各算一遍，
        # validate → parse 每轮回退再乘一次，而三份结果本来就来自同一份 `raw_text`
        sections = split_sections(raw_text)
        timeline = extract_timeline(raw_text, sections=sections)
        skills = extract_skills(raw_text, sections=sections)
        projects = extract_projects(raw_text, sections=sections)

        errors = [f"parse: {warning}" for warning in document.warnings]
        if reused is not None:
            errors.append(REUSED_SOURCE_NOTE)
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
            # 下一次回退的判据：正文是谁读的、读的哪一个文件（mtime+size）
            "source_parser": document.parser,
            "source_prompt_version": document.prompt_version,
            "source_stamp": _file_stamp(path),
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
) -> ReadOutcome:
    """图片直接交给视觉路径；PDF 报"这是扫描件"时也转过去——那是扫描件唯一出路。"""
    if path.suffix.lower() in IMAGE_SUFFIXES:
        return _as_outcome(await ImageParser(llm=llm).parse(path), from_ocr=True)
    try:
        return _as_outcome(await asyncio.to_thread(registry.parse, path))
    except ParseError as exc:
        if exc.code != "parse_scanned_image":
            raise
        return _as_outcome(await ImageParser(llm=llm).parse(path), from_ocr=True)


def _as_outcome(document: ParsedDocument, *, from_ocr: bool = False) -> ReadOutcome:
    return ReadOutcome(
        raw_text=document.raw_text,
        parser=document.parser,
        prompt_version=document.prompt_version,
        warnings=document.warnings,
        from_ocr=from_ocr,
    )


def _validate(fields: dict[str, Any], document: ReadOutcome, ner: NerResult) -> dict[str, Any]:
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


def _prompt_stamp(document: ReadOutcome, ner_version: str) -> str:
    """本次真用过的模板，形如 `ner@v1+ocr@v1`；没调 LLM 时是空串。"""
    used = sorted({v for v in (document.prompt_version, ner_version) if v})
    return "+".join(used)
