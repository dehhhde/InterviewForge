"""简历上传/解析/存储/查询〔阶段 2 建壳，阶段 5 填充〕

本文件目前只有**校验部分**〔M2 · §10.4.2〕：把 `parsed` 通道的结构化结果按四条规则
过一遍，能机械修好的就地修好（记进 `repairs`），修不了的原样报给 `validate_node`
（记进 `issues`，驱动 parse 的有界回退）。上传/存储/查询等 rest 部分随阶段 2 的接口补齐。

为什么规则层是纯函数、不碰 LLM 也不碰数据库：
- 校验层是回退路由的唯一输入，它必须是确定性、可单测、零成本的；
- LLM 反思只在规则全过之后跑（见 `validate_node`），顺序反了就是拿模型替正则兜底。

`日期不倒挂` 这条为什么要重跑一遍 `extract_timeline`：倒挂的区间根本进不了 `parsed`
（`WorkExperience._check_date_order` 会 raise），只看 `parsed` 等于永远查不出这个毛病。
抽取器把这类行记进 `TimelineResult.dropped`，校验层就从这里取。
"""

import asyncio
import contextlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, get_args
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.exceptions.base import ConflictError, ValidationFailedError
from app.extractors.base import evidence_belongs
from app.extractors.timeline_extractor import DEGREE_WORDS, TimelineResult, extract_timeline
from app.models.resume import Resume
from app.models.task import KIND_RESUME_PARSING, Task
from app.parsers.image_parser import IMAGE_SUFFIXES
from app.repositories.base import keyword_filter
from app.repositories.interview_repository import InterviewRepository
from app.repositories.resume_repository import ResumeRepository
from app.schemas.resume import DEGREE_ALIASES, Degree
from app.services import task_service
from app.utils.db import session_scope

#: 自称年限与时间线年限的容差。只有年份的区间补齐成 1/1 与 12/31，误差本就接近一年
YEARS_TOLERANCE = 1.0

#: `parsed` 里带 `evidence` 的列表字段，证据核对逐个过一遍
_EVIDENCED_LISTS = ("skills", "educations", "work_experiences", "projects")
#: Degree 枚举的全部合法取值，用来接住不是从 `ResumeSchema` 走出来的 parsed
_DEGREE_VALUES = frozenset(get_args(Degree))


@dataclass(frozen=True)
class ValidationOutcome:
    """一次规则校验的结果。`parsed` 是修好的副本，调用方覆盖写回通道。"""

    parsed: dict[str, Any]
    issues: tuple[str, ...] = ()
    repairs: tuple[str, ...] = ()


def validate_resume(
    raw_text: str,
    parsed: dict[str, Any] | None,
    *,
    today: date | None = None,
) -> ValidationOutcome:
    """规则先行的四道检查。产出 `issues`（判罚）与 `repairs`（已自动修好）。

    传入的 `parsed` 不会被改动：函数在副本上修，方便调用方对比与重放。
    """
    if parsed is None:
        return ValidationOutcome(
            parsed={}, issues=("还没有抽取结果，校验无从下手",), repairs=()
        )

    fixed = dict(parsed)
    issues: list[str] = []
    repairs: list[str] = []

    _prune_without_evidence(raw_text, fixed, repairs)
    timeline = extract_timeline(raw_text, today=today)
    _check_required(fixed, issues)
    _check_date_order(timeline, issues)
    _check_degree_enum(fixed, issues)
    _reconcile_years(timeline, fixed, issues, repairs)

    return ValidationOutcome(parsed=fixed, issues=tuple(issues), repairs=tuple(repairs))


def _prune_without_evidence(
    raw_text: str, fixed: dict[str, Any], repairs: list[str]
) -> None:
    """证据不在原文里就是幻觉，直接删掉该条目。

    删而不报 issue：抽取层的铁律是"无证据即丢"，出现在 `parsed` 里说明它绕过了核对，
    修掉即可，不值得为它重跑一遍 parse。
    """
    for key in _EVIDENCED_LISTS:
        items = fixed.get(key)
        if not isinstance(items, list):
            continue
        kept = [
            item
            for item in items
            if isinstance(item, dict) and evidence_belongs(raw_text, str(item.get("evidence", "")))
        ]
        if len(kept) == len(items):
            continue
        removed = len(items) - len(kept)
        fixed[key] = kept
        repairs.append(
            f"{key} 里有 {removed} 条的证据在原文里找不到，已删除（疑似抽取错位）"
        )


def _check_required(fixed: dict[str, Any], issues: list[str]) -> None:
    """必填：姓名与年限。`ResumeSchema` 已经挡过一轮，这里防的是别处写进来的 `parsed`。"""
    name = fixed.get("name")
    if not (isinstance(name, str) and name.strip()):
        issues.append("缺少姓名，后面的画像和出题都无处挂靠")
    years = fixed.get("years_experience")
    if not isinstance(years, (int, float)):
        issues.append("缺少工作年限，难度分档没有依据")


def _check_date_order(timeline: TimelineResult, issues: list[str]) -> None:
    """日期不倒挂：从抽取器丢弃的行里认。"""
    for entry in timeline.dropped:
        if entry.reason != "inverted":
            continue
        issues.append(
            f"时间段倒挂（{entry.range_text} 结束早于开始），这一段没进结果：{entry.line[:40]}"
        )


def _check_degree_enum(fixed: dict[str, Any], issues: list[str]) -> None:
    """学历枚举：值必须在 `Degree` 里；在枚举表之外但原文写了学历字样的，说明别名表漏收。"""
    for item in fixed.get("educations") or []:
        if not isinstance(item, dict):
            continue
        degree = item.get("degree")
        if degree not in _DEGREE_VALUES:
            issues.append(f"学历值 {degree!r} 不在枚举 {sorted(_DEGREE_VALUES)} 里")
            continue
        if degree != "other":
            continue
        # 归到 other 又确实在原文里认出了字样，那是别名表的问题，不该由下游猜
        evidence = str(item.get("evidence", ""))
        # 拉丁字样（MBA/EMBA）大小写不敏感：证据里写 `mba` 也算认得，别为它报 issue
        word = next(
            (w for w in DEGREE_WORDS if w in evidence or w in evidence.upper()),
            None,
        )
        if word is not None and word not in DEGREE_ALIASES:
            school = str(item.get("school", ""))[:20]
            issues.append(f"{school} 的学历字样「{word}」不在别名表里，已按 other 归档，请确认")


def _reconcile_years(
    timeline: TimelineResult, fixed: dict[str, Any], issues: list[str], repairs: list[str]
) -> None:
    """年限与时间线自洽：拿时间线复算，差太多就报，压根没填就补。"""
    computed = timeline.years_experience
    claimed = fixed.get("years_experience")
    if not isinstance(claimed, (int, float)):
        return
    if computed is None:
        if claimed == 0 and timeline.work_experiences:
            issues.append("有工作条目却没有年限，后面的难度分档会失真")
        return

    target = float(computed.value)
    gap = abs(float(claimed) - target)
    if gap <= YEARS_TOLERANCE:
        return
    if claimed == 0:
        # parse 阶段没有任何日期区间线索时按 0 占位；现在时间线算出来了，直接补
        fixed["years_experience"] = target
        repairs.append(f"年限原本按 0 占位，按时间线补成 {computed.value} 年")
        return
    issues.append(
        f"年限 {round(float(claimed), 1)} 年与日期区间算出的 {computed.value} 年差 "
        f"{round(gap, 1)} 年，超出 {YEARS_TOLERANCE} 年容差，请核对是不是漏了或重了段落"
    )

# --- 上传受理与读取〔M6〕---
#
# 上面是纯函数规则层，从这里开始才碰会话与文件系统。上传入口与图里的 parse 用同一份
# 抽取代码（`app/tasks/resume_parsing.py` 直接调 parse 节点），本文件只负责
# "文件落盘 + 建行 + 建任务"，不做解析——解析是后台的事，响应要立刻带 task_id 回来。

#: 同步解析器认的扩展名（与 `parse_node.default_registry` 注册的两家一致）。
_SYNC_SUFFIXES = (".pdf", ".docx")


def allowed_suffixes(settings: Settings | None = None) -> tuple[str, ...]:
    """上传入口允许的文件后缀。

    图片只在 vision 模型已选型时受理：文本模型"读"图不会报错，只会编出一份看起来很完整
    的简历（`settings.llm_model_vision` 的注释），与其等后台任务失败，不如上传时就拒。
    """
    cfg = settings or get_settings()
    if cfg.llm_model_vision:
        return (*_SYNC_SUFFIXES, *IMAGE_SUFFIXES)
    return _SYNC_SUFFIXES


async def accept_upload(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    file_name: str,
    data: bytes,
    settings: Settings | None = None,
) -> tuple[Resume, Task]:
    """落盘 + 插 `resume`/`task` 两行并**当场提交**，返回给 API 挂后台任务用。

    为什么这里自己开事务、不像别的写入那样只 flush：后台协程在 `get_db` 收尾**之前**就
    开始跑（实测，ADR-0011），拿请求事务的话它第一件事就是"简历行不存在"。提交点归服务层
    仍然符合 §3 的分工——"提交时机归调用方"，此处的调用方就是这段受理逻辑本身。
    返回的 ORM 实例带着 `expire_on_commit=False`，事务结束后属性依然可读。

    落盘名固定 `{resume_id}{suffix}`——用户文件名不参与路径构造，穿越与覆盖都在设计上
    关门；`file_name` 原样只进展示列（截到列宽 255）。
    """
    cfg = settings or get_settings()
    suffix = Path(file_name).suffix.lower()
    if suffix not in allowed_suffixes(cfg):
        raise ValidationFailedError(
            "不支持的简历文件类型",
            code="upload_bad_suffix",
            details={"file_name": file_name, "supported": list(allowed_suffixes(cfg))},
        )
    if not data:
        raise ValidationFailedError(
            "上传的文件是空的", code="upload_empty_file", details={"file_name": file_name}
        )

    resume_id = uuid4().hex
    stored = f"{resume_id}{suffix}"
    cfg.upload_dir.mkdir(parents=True, exist_ok=True)
    # PDF 是二进制整写，没有编码问题；但要等整个响应周期写完才算落盘，
    # 所以放线程里跑，别让事件循环抱着磁盘等（AGENTS.md §2 的理由在这同样成立）。
    await asyncio.to_thread((cfg.upload_dir / stored).write_bytes, data)

    async with session_scope(sessionmaker) as session:
        resume = await ResumeRepository(session).create(
            id=resume_id, stored_path=stored, file_name=file_name[:255]
        )
        task = await task_service.create_task(
            session, kind=KIND_RESUME_PARSING, ref_id=resume_id
        )
    return resume, task


async def fetch_resume(session: AsyncSession, resume_id: str) -> Resume | None:
    return await ResumeRepository(session).get_by_id(resume_id)


async def list_resumes(
    session: AsyncSession, *, limit: int = 50, offset: int = 0, q: str = ""
) -> tuple[Sequence[Resume], int]:
    """一页简历 + **同一条件下**的总行数〔T-303〕。

    搜索列只有 `name` / `file_name`：`raw_text` 与 `parsed` 是正文，不是标识。
    拿正文做 LIKE 会把"搜'林可'"变成"搜所有提到林可的简历"，而且 SQLite 上没有
    全文索引，那种查询只会把整表扫一遍。
    """
    repository = ResumeRepository(session)
    filters = keyword_filter(Resume, q, ("name", "file_name"))
    rows = await repository.list(limit=limit, offset=offset, filters=filters)
    return rows, await repository.count(filters=filters)


async def rename_resume(session: AsyncSession, resume_id: str, name: str) -> Resume | None:
    """只改展示名。`raw_text` / `parsed` 没有写入口，理由见 `ResumeUpdateRequest` 的 docstring
    （它们是证据链的真相源，手改一处，报告里每条"依据出处"就对不上原文了）。
    """
    return await ResumeRepository(session).update(resume_id, name=name[:120])


async def delete_resume(
    session: AsyncSession,
    resume_id: str,
    *,
    force: bool = False,
    settings: Settings | None = None,
) -> bool:
    """删简历并删落盘文件。名下有报告时默认 409 拒绝。

    为什么默认拒绝：`interview.resume_id` 是 `ON DELETE CASCADE`（ADR-0007 的原始取舍——
    没了简历的报告没有意义），所以这一删会把报告与题目行一起带走，而本仓库不是 git、
    库里也没有软删列，误删没有任何补救手段。`force=true` 才放行。
    （实测过一次代价：给 `interview` 加列时走 `batch_alter_table` 重建表，级联删掉了 30 行题目。）

    文件放在 `session.delete` 之后删、且忽略失败：行都没删成却先把原件删了是最坏的顺序；
    反过来留一个孤儿文件只是占盘，不影响任何读取。
    """
    repository = ResumeRepository(session)
    resume = await repository.get_by_id(resume_id)
    if resume is None:
        return False

    reports = await InterviewRepository(session).list_for_resume(resume_id)
    if reports and not force:
        raise ConflictError(
            "这份简历名下还有报告，删除会连报告一起没；确认请加 force=true",
            details={"resume_id": resume_id, "interview_count": len(reports)},
        )

    stored_path = resume.stored_path
    deleted = await repository.delete(resume_id)
    if deleted and stored_path:
        cfg = settings or get_settings()
        # 只信 `stored_path` 的相对形状（上传时按 `{resume_id}{suffix}` 写的），不拼用户文件名
        target = cfg.upload_dir / Path(stored_path).name
        await asyncio.to_thread(_remove_quietly, target)
    return deleted


def _remove_quietly(path: Path) -> None:
    """删落盘原件，失败就当没删。行已经删了，留一个孤儿文件只是占盘；
    反过来（先删文件再删行）才会造出"库里有、盘上没有"的坏行。"""
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)
