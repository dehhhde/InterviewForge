"""项目经历抽取：按项目名切块，再按子标题分"描述 / 技术栈 / 成果"〔阶段 5 · §8.6.3〕

判"这一行是项目开头"用的是版式而不是词典：项目名那一行后面会紧跟 `项目描述`/`技术栈`
这类子标题，而且它自己不是子标题、不是列表项。带日期的行是工作履历，归
`timeline_extractor`，这里不收。子标题只当分块标记，不入正文。

`highlights` 收编号要点（`1.`、`2、`、`- `），散文归 `summary`——分开存是因为 M5 出题时
要拿要点当题干里的"你提到 XX，具体怎么做的"。但**编号行有两类**：`1. 权限管理体系`
是分组标题，`1、设计自适应直传策略` 才是成果条目，判据见 `_is_numbered_subheading`。
前者只开新桶、不进 `highlights`（实测：真简历 5 行分组标题混进要点后，模型评审有一半意见
在报这件事，而出题会拿"业务能力"这种光秃秃的标签当题干）。

每条都带 `evidence`（整块原文，空白折叠过），并且必须能在 `raw_text` 里核对上；
对不上说明切分拼错了行，宁可丢弃并记进 `notes`。
"""

import re
from dataclasses import dataclass, field

from app.extractors.base import collapse, evidence_belongs, join_wrapped_lines
from app.extractors.sections import SectionMap, SubName, header_kind, is_bullet, split_sections
from app.extractors.timeline_extractor import parse_range
from app.schemas.resume import ProjectExperience

#: 出现在项目块开头的角色行前缀，单独存进 `role`，不混进描述
_ROLE_PREFIXES = ("担任", "负责", "角色：", "职位：")
_MAX_EVIDENCE_CHARS = 400
#: 项目名不会写成整句，带句读的短行是换行剩下的正文尾巴
_PROSE_MARKS = "。；，！？;!?"
_TITLE_MAX_CHARS = 40
#: 编号行去掉编号后超过这个长度就当成果条目（"1、设计基于文件大小的自适应直传…"）
_SUBHEADING_MAX_CHARS = 14
#: 分组标题是名词短语，句内不该有分隔标点；出现标点就说明这是一句话、是一个条目
_SUBHEADING_MARKS = "，,、；;：:。！!？?"
#: 编号 `1.` / `2、` / `3)`；`-`/`•` 这类项目符号不是编号，不参与本判据
_NUMBER_MARKER_RE = re.compile(r"^\s*\d{1,2}\s*[.、)）]\s*(?P<body>.*)$")
#: 成果条目几乎总以动作词开头。表只收"条目会带、标题不会带"的词：
#: 判不准时按条目处理，那等于改动前的行为（宁松不紧，少丢真要点）。
_ACTION_LEADING = (
    "负责", "参与", "主导", "梳理", "对接", "保障", "提升", "降低", "支持", "采用",
    "基于", "使用", "运用", "设计", "实现", "搭建", "构建", "完成", "开发", "引入",
    "落地", "优化", "提供", "输出", "打通", "封装", "沉淀", "推动", "研发", "覆盖",
)


def _is_numbered_subheading(text: str) -> bool:
    """`1. 权限管理体系` 这类编号**分组标题**；`1、设计自适应直传策略` 不是。"""
    found = _NUMBER_MARKER_RE.match(text)
    if found is None:
        return False
    body = found.group("body").strip()
    if not body or len(body) > _SUBHEADING_MAX_CHARS:
        return False
    if any(mark in body for mark in _SUBHEADING_MARKS):
        return False
    return not body.startswith(_ACTION_LEADING)


@dataclass(frozen=True)
class ProjectResult:
    projects: list[ProjectExperience] = field(default_factory=list)
    notes: tuple[str, ...] = ()

    def as_resume_fields(self) -> dict[str, object]:
        return {"projects": [p.model_dump(mode="json") for p in self.projects]}


def extract_projects(raw_text: str, *, sections: SectionMap | None = None) -> ProjectResult:
    """从"项目经历"栏目切块。栏目不在就直说，不拿全文硬猜项目名。

    `sections` 是调用方切好的栏目表（T-208：一次 parse 三个抽取器共用一份），省略则自切。
    """
    resolved_sections = sections if sections is not None else split_sections(raw_text)
    lines = resolved_sections.lines_of("project")
    if not lines:
        return ProjectResult(notes=("没找到项目经历栏目，项目相关的问题只能从工作描述里找线索",))

    notes: list[str] = []
    projects: list[ProjectExperience] = []
    blocks, guessed_names = _split_blocks(lines)
    if guessed_names:
        notes.append("项目栏目里没有日期区间，项目名是按版式猜的（紧跟子标题的短行），可能不准")
    for block, name in blocks:
        if name is None:
            notes.append(f"这块项目正文找不到项目名，已丢弃：{collapse(block[0])[:40]}")
            continue
        item = _build_project(block, name)
        if not evidence_belongs(raw_text, item.evidence):
            notes.append(f"项目证据对不上原文，已丢弃：{item.name}")
            continue
        projects.append(item)
    if not projects and not notes:
        notes.append("项目栏目里有内容但一块都没切出来，多半是版式不按「项目名 + 子标题」走")
    return ProjectResult(projects=projects, notes=tuple(notes))



def _split_blocks(lines: list[str]) -> tuple[list[tuple[list[str], str | None]], bool]:
    """一块 = 一个项目名行 + 它后面直到下一个项目名行的所有行；返回（块，名字是否靠猜）。

    标题行分两级判：带日期区间的最可靠（`OA办公自动化系统  2025.07-2026.07`），
    整段一个区间都没有才退回版式判据（短行、句读干净、紧跟子标题）。
    """
    dated = [index for index, line in enumerate(lines) if parse_range(line) is not None]
    guessed = not dated
    titles = dated or [index for index in range(len(lines)) if _is_wrapped_title(lines, index)]
    if not titles:
        return ([(lines, None)] if lines else []), guessed
    blocks: list[tuple[list[str], str | None]] = []
    if titles[0] > 0:
        blocks.append((lines[: titles[0]], None))
    for position, start in enumerate(titles):
        stop = titles[position + 1] if position + 1 < len(titles) else len(lines)
        block = lines[start:stop]
        blocks.append((block, _title_of(block[0])))
    return blocks, guessed


def _title_of(line: str) -> str:
    """项目名取日期区间前面的那一截；区间本身留在 `evidence` 里，不当名字的一部分。"""
    found = parse_range(line)
    if found is None:
        return collapse(line)
    return collapse(found.head) or collapse(line)


def _is_wrapped_title(lines: list[str], index: int) -> bool:
    """自己不像正文（短、无句读、不是列表项），且一到两行内出现子标题。"""
    line = lines[index]
    section, sub = header_kind(line)
    text = collapse(line)
    if section is not None or sub is not None or is_bullet(line):
        return False
    if len(text) > _TITLE_MAX_CHARS or any(mark in text for mark in _PROSE_MARKS):
        return False
    for nxt in lines[index + 1 : index + 3]:
        if not is_bullet(nxt) and len(collapse(nxt)) > 20:
            return False  # 中间隔了正文，说明这行不是标题
        if header_kind(nxt)[1] is not None:
            return True
    return False


def _build_project(block: list[str], name: str) -> ProjectExperience:
    """子标题切出来的桶：描述与技术栈拼进 `summary`，成果与编号要点拼进 `highlights`。

    编号分组标题只切桶、不入 `highlights`，也不写 `notes`：真简历的项目段普遍用这种写法，
    为一条正常的版式惯例报流水账会把健康路径的 `errors` 变成噪音；标题本身仍在 `evidence` 里。
    """
    summary: list[str] = []
    stack: list[str] = []
    highlights: list[str] = []
    role: str | None = None
    # 成果桶就是 `highlights` 本身：先前两份分别收、最后 `highlights += outcome`，
    # 会让"编号条目在前、被换行切断的尾巴在后"，模型评审据此报"错位"（T-102 录制件第 3 条）。
    # 归同一个列表按文档顺序收，成员不变、顺序回到原文顺序。
    buckets: dict[SubName, list[str]] = {"summary": summary, "stack": stack, "outcome": highlights}
    bucket = summary

    for line in block[1:]:
        text = collapse(line)
        if not text:
            continue
        section, sub = header_kind(line)
        if sub is not None:
            bucket = buckets[sub]
            continue
        if section is not None:  # 段标题混进项目段（词典把它认成了大栏目）
            continue
        if is_bullet(line):
            if _is_numbered_subheading(text):
                bucket = highlights  # 分组标题之后的正文跟着这一组走
                continue
            highlights.append(text)
            continue
        if role is None and bucket is summary and text.startswith(_ROLE_PREFIXES):
            role = text
            continue
        bucket.append(text)

    # 桶内是"被 PDF 换行劈开的同一段话"，按换行连接判据拼；桶间（描述 / 技术栈）是真话题
    # 边界，才用"；"。技术栈那一行的前缀也是话题标签，不属于原文。
    parts = [join_wrapped_lines(summary)] if summary else []
    if stack:
        parts.append(f"技术栈：{join_wrapped_lines(stack)}")

    evidence = collapse(" ".join(block))[:_MAX_EVIDENCE_CHARS]
    return ProjectExperience(
        name=name,
        role=role,
        summary="；".join(parts) or None,
        highlights=highlights,
        evidence=evidence,
    )
