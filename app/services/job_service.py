"""岗位服务：读取 + 创建/更新/删除 + JD 文件受理 + 图片识别草稿〔阶段 3 建壳，写入侧补齐〕

分层照旧：这里不写 SQL、不 commit（提交由调用方决定——HTTP 侧是 `get_db`，脚本侧是
`session_scope`）。写入走 `JobRepository`。图片那条路（`draft_job_from_image`）更彻底：
**根本不碰库**，只回一份草稿，落库由人确认后走 `create_job`。

为什么 `required_skills` 由词典派生而不是交给模型：`match` 比对岗位要求用的就是
`SKILL_ALIASES` 这一本词典（`skill_mentions_in` 的 docstring 明说"JD 侧复用它是刻意的"）。
上传时派生一遍、match 时再判一遍，两侧同一判据；换成 LLM 抽技能，就会出现"入库的技能名
和比对时认出的技能名对不上"的假缺口。用户显式给了技能清单时以用户给的为准，只做归一。

JD 文件上传是**同步**的，和简历上传（202 + 后台任务）不一样：整条链路只读文件和跑词典，
零 LLM 调用，没有需要等的东西，也就没必要造 task 行。图片那条路确实要等模型（1 次读图 +
至多 1 次补字段），但它**不产出任何行**——一次来回几十秒的等待换一个草稿，
比造一个 task 行、再让页面轮询"草稿好了没"要直白。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions.base import ConflictError, ValidationFailedError
from app.extractors.job_extractor import JobFieldExtractor
from app.extractors.skill_extractor import skill_mentions_in
from app.models.job import Job
from app.parsers.base import ParserRegistry
from app.parsers.docx_parser import DocxParser
from app.parsers.image_parser import IMAGE_SUFFIXES, ImageParser
from app.parsers.pdf_parser import PdfParser
from app.repositories.job_repository import JobRepository
from app.schemas.job import JobCreateRequest, JobDraftOut, JobUpdateRequest
from app.utils.llm_client import LLMClient

#: 纯文本类 JD。`.md` 也在其中：招聘站复制出来的 JD 常带 markdown 列表符号，
#: 而 `split_requirements` 本来就按行切，`- ` 前缀不影响判定。
_TEXT_SUFFIXES = (".txt", ".md")


def _document_registry() -> ParserRegistry:
    """二进制 JD 用的解析器。

    不在这里取 `parse_node.default_registry()`：那是 `agents` 层的东西，服务层反向 import
    节点就把"节点不碰数据库"之外的分层约定也搅了。两家同步解析器在这里列一遍，代价是一行。
    """
    return ParserRegistry([PdfParser(), DocxParser()])


def allowed_suffixes() -> tuple[str, ...]:
    return (*_TEXT_SUFFIXES, *_document_registry().supported)


def allowed_image_suffixes() -> tuple[str, ...]:
    """图片录入受理的扩展名。走的是 `ImageParser`，与简历扫描件同一套判据。

    不含 `.pdf`：PDF 由 `POST /jobs/upload` 受理，只有 `PdfParser` 明确报"这一页是扫描图"
    的原件才值得走视觉模型，而那是 ADR-0008 预留的账，不在这里顺手开。
    """
    return IMAGE_SUFFIXES


def derive_required_skills(jd_text: str) -> list[str]:
    """从 JD 原文里认出要求的技能（小写正名，按出现顺序去重）。

    逐行走 `skill_mentions_in`：`SKILL_ALIASES` 的匹配是"一行内"的判据，
    整段喂进去会让跨行的别名粘连，也和 `match` 看到的粒度不一致。
    """
    out: list[str] = []
    seen: set[str] = set()
    for line in jd_text.splitlines():
        for mention in skill_mentions_in(line):
            key = mention.name.lower()
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


def _normalize_skills(names: Sequence[str]) -> list[str]:
    """用户显式给的清单只做小写归一与去重，不补也不删技能——那是人的判断。"""
    out: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = name.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def decode_text(data: bytes) -> str:
    """把 JD 字节流解成文本：先 UTF-8（带 BOM 也吃），再退到 GB18030。

    为什么必须留 GB 这条路：这台机器的记事本另存 .txt 默认就是 GBK，一份中文 JD 按
    UTF-8 硬解会当场 `UnicodeDecodeError`，而报错原因（编码）对用户完全不可见。
    GB18030 是 GBK 的超集，放最后兜；两种都解不动才判上传失败。
    """
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValidationFailedError(
        "JD 文件既不是 UTF-8 也不是 GBK，解不出来",
        code="jd_encoding_unknown",
        details={"size": len(data), "tried": ["utf-8", "gb18030"]},
    )


async def read_jd_file(*, file_name: str, data: bytes) -> str:
    """受理上传的 JD 文件，返回纯文本。不落盘：`job.jd_text` 存的就是文本本身。

    简历要把原件留着（后台 parse 按 `<upload_dir>/<id><后缀>` 找回原件），
    岗位不需要——它没有"稍后再解析一遍"的阶段，抽完技能原文就够下游逐字引用了。
    """
    suffix = Path(file_name).suffix.lower()
    if suffix not in allowed_suffixes():
        raise ValidationFailedError(
            "不支持的 JD 文件类型",
            code="jd_bad_suffix",
            details={"file_name": file_name, "supported": list(allowed_suffixes())},
        )
    if not data:
        raise ValidationFailedError(
            "上传的文件是空的", code="jd_empty_file", details={"file_name": file_name}
        )
    if suffix in _TEXT_SUFFIXES:
        return decode_text(data)

    # PDF/DOCX 的解析器签名是 `parse(path)`，而它只认扩展名不认来源，所以借一个临时目录
    # 把字节流还原成文件。解析器是同步且 CPU 密集的（PDF 抽文本），两次都丢进线程池。
    registry = _document_registry()
    workdir = Path(tempfile.mkdtemp())
    path = workdir / f"incoming{suffix}"
    try:
        await asyncio.to_thread(path.write_bytes, data)
        document = await asyncio.to_thread(registry.parse, path)
    finally:
        await asyncio.to_thread(shutil.rmtree, workdir, ignore_errors=True)
    return document.raw_text


async def list_jobs(
    session: AsyncSession, *, limit: int = 50, offset: int = 0
) -> Sequence[Job]:
    return await JobRepository(session).list(limit=limit, offset=offset)


async def get_job(session: AsyncSession, job_id: str) -> Job | None:
    """找不到返回 `None`，404 的判断留给端点（repository 的约定一路传上来）。"""
    return await JobRepository(session).get_by_id(job_id)


async def create_job(session: AsyncSession, payload: JobCreateRequest) -> Job:
    """插一个岗位。自然键 `title + company` 撞已存在就抛 `ConflictError`。"""
    repository = JobRepository(session)
    duplicate = await repository.get_by_title_and_company(payload.title, payload.company)
    if duplicate is not None:
        raise ConflictError(
            "同名同公司的岗位已经存在，要改它请用 PUT /jobs/{id}",
            details={"job_id": duplicate.id, "title": payload.title, "company": payload.company},
        )
    skills = (
        _normalize_skills(payload.required_skills)
        if payload.required_skills
        else derive_required_skills(payload.jd_text)
    )
    return await repository.create(
        title=payload.title,
        company=payload.company,
        jd_text=payload.jd_text,
        required_skills=skills,
        min_years=payload.min_years,
    )


async def create_job_from_upload(
    session: AsyncSession,
    *,
    file_name: str,
    data: bytes,
    title: str,
    company: str = "",
    min_years: float = 0.0,
) -> Job:
    """`POST /jobs/upload`：读文件 → 取文本 → 走 `create_job` 的同一条路。

    技能清单一律派生：文件里没有地方让人显式列技能，让人为了填表单再敲一遍技能名
    不如让词典认一遍；真要改，随后 PUT 一次。
    """
    jd_text = await read_jd_file(file_name=file_name, data=data)
    # 表单字段没有 pydantic 兜着（`Form(...)` 只挡空串），长度规则在服务层自己判一次：
    # 直接 `JobCreateRequest(...)` 的话 ValidationError 会冒成 500，而这是用户的输入问题。
    clean_title = title.strip()
    clean_text = jd_text.strip()
    if not clean_title:
        raise ValidationFailedError(
            "岗位名称不能为空", code="jd_missing_title", details={"file_name": file_name}
        )
    if len(clean_text) < 5:
        raise ValidationFailedError(
            "文件里读出的 JD 太短，出题没有依据",
            code="jd_too_short",
            details={"file_name": file_name, "characters": len(clean_text)},
        )
    payload = JobCreateRequest(
        title=clean_title[:200],
        company=company.strip()[:200],
        jd_text=jd_text,
        min_years=min_years,
    )
    return await create_job(session, payload)


async def draft_job_from_image(
    *, file_name: str, data: bytes, llm: LLMClient
) -> JobDraftOut:
    """`POST /jobs/inspect-image`：图片 → 视觉模型抄成文本 → 认字段 → 回草稿。

    与 `create_job_from_upload` 的三点不同，都是"图片要过模型"带来的：

    1. **不落库、不查库**：识别结果先给人过一遍，人点保存才走 `create_job` 那条老路，
       于是 409/422 的语义与 JSON 建岗完全一致；
    2. 原件不留盘：与 JD 文件同理，岗位没有"稍后再解析一遍"的阶段；
    3. 字段是**认出来的**，认不出就留空（`missing_required` 里点名），不猜。

    读图复用 `ImageParser`（简历扫描件同一个类、同一个 `ocr` 模板），补字段复用
    `JobFieldExtractor`（先规则后模型）。所以这张图最多花 2 次模型调用。
    """
    suffix = Path(file_name).suffix.lower()
    if suffix not in allowed_image_suffixes():
        raise ValidationFailedError(
            "不支持的图片文件类型",
            code="job_image_bad_suffix",
            details={"file_name": file_name, "supported": list(allowed_image_suffixes())},
        )
    if not data:
        raise ValidationFailedError(
            "上传的文件是空的", code="jd_empty_file", details={"file_name": file_name}
        )

    # `ImageParser.parse` 的入参是 Path（它要按扩展名分流图片与 PDF），所以借临时目录
    # 把字节流还原成文件，与 `read_jd_file` 同一条套路。写盘是同步 IO，丢线程池。
    workdir = Path(tempfile.mkdtemp())
    path = workdir / f"incoming{suffix}"
    try:
        await asyncio.to_thread(path.write_bytes, data)
        document = await ImageParser(llm=llm).parse(path)
    finally:
        await asyncio.to_thread(shutil.rmtree, workdir, ignore_errors=True)

    jd_text = document.raw_text.strip()
    if len(jd_text) < 5:
        raise ValidationFailedError(
            "图片里读出的 JD 太短，出题没有依据",
            code="jd_too_short",
            details={"file_name": file_name, "characters": len(jd_text)},
        )

    fields = await JobFieldExtractor(llm=llm).extract(jd_text)
    return JobDraftOut(
        title=fields.title.value if fields.title else "",
        company=fields.company.value if fields.company else "",
        jd_text=jd_text,
        required_skills=derive_required_skills(jd_text),
        min_years=float(fields.min_years.value) if fields.min_years else 0.0,
        missing_required=[] if fields.title else ["title"],
        notes=[*document.warnings, *fields.notes],
    )


async def update_job(session: AsyncSession, job_id: str, payload: JobUpdateRequest) -> Job | None:
    """改指定字段。给了 `jd_text` 又没给 `required_skills` 时重新派生一遍——
    留着旧清单等于让 `match` 拿过期的要求去比新的 JD。"""
    repository = JobRepository(session)
    current = await repository.get_by_id(job_id)
    if current is None:
        return None
    values = payload.model_dump(exclude_unset=True)
    title = str(values.get("title", current.title))
    company = str(values.get("company", current.company))
    if (title, company) != (current.title, current.company):
        clash = await repository.get_by_title_and_company(title, company)
        if clash is not None and clash.id != current.id:
            raise ConflictError(
                "目标岗位名与另一个岗位撞了自然键",
                details={"job_id": clash.id, "title": title, "company": company},
            )

    updates: dict[str, object] = {
        field: values[field] for field in ("title", "company", "min_years", "jd_text")
        if field in values
    }
    if "jd_text" in updates and "required_skills" not in values:
        updates["required_skills"] = derive_required_skills(str(updates["jd_text"]))
    if values.get("required_skills") is not None:
        updates["required_skills"] = _normalize_skills(values["required_skills"])
    return await repository.update(job_id, **updates)


async def delete_job(session: AsyncSession, job_id: str) -> bool:
    """删岗位。返回是否真删了。

    历史报告不会跟着消失：`interview.job_id` 是 `ON DELETE SET NULL`，而报告头里还存着
    生成当时的 `job_title` / `job_company` 快照——"这份卷子按哪个岗位出的"是报告的事实。
    """
    return await JobRepository(session).delete(job_id)
