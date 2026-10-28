"""报告导出：一份 Markdown 的**唯一渲染处**〔S3 · T-302〕

分层位置：这里只读（repository + 只读的 checkpoint 线程），不写任何 SQL、不改任何业务行。

为什么这份渲染原先在 `scripts/export_report_md.py` 里、现在搬进服务层：控制台此前自己
另写了一套 Markdown（`frontend/src/utils/markdown.ts`），同一个文件名约定、不同的内容——
"两套分叉的导出，同名不同内容"是 BACKLOG P3-10 记的那笔账。渲染逻辑只有一个出处，
端点（`GET /interviews/{id}/export.md`）、脚本与将来的打印入口才可能给出同一份文件。

**两处数据来源，各自诚实**：题目与报告头以业务库为准（`review` 收尾写下的终态），
画像五维 / JD 缺口 / trace 这些"分析过程"只在 checkpoint 的图终态里——`interview.plan`
存的是**去标识后的配额表**（`plan_node.sanitize()`，不含姓名与逐字证据），
和图里那份不是同一对象。所以图终态缺席时这几节整段不渲染，并在文首留一句说明，
而不是拿脱敏表冒充全过程。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.workflow import read_thread_state
from app.models.interview import Interview
from app.models.job import Job
from app.models.question import Question
from app.models.resume import Resume
from app.repositories.interview_repository import InterviewRepository
from app.repositories.job_repository import JobRepository
from app.repositories.question_repository import QuestionRepository
from app.repositories.resume_repository import ResumeRepository
from app.services.question_service import evidence_ratio

#: 渲染用的中文对照。放服务层而不是前端：导出文件是服务端产物，两份词表迟早分叉。
DIFFICULTY_CN = {"easy": "简单", "medium": "中等", "hard": "较难"}
SOURCE_CN = {"resume": "简历", "jd": "JD", "generic": "通用"}
LEVEL_CN = {"matched": "命中", "partial": "部分命中", "missing": "无证据", "unverified": "待确认"}
DEGREE_CN = {
    "college": "大专",
    "bachelor": "本科",
    "master": "硕士",
    "phd": "博士",
    "other": "其他",
}

#: 图终态缺席时写在文首的那句说明。不许把它省成"数据不足"——读的人要知道缺的是哪几节、
#: 以及为什么缺（旧报告没有可寻址的线程 id，新报告的是 checkpoint 库被清理过）。
NO_STATE_NOTE = (
    "checkpoint 里找不到这份报告的图终态，所以只有题目，没有画像/缺口/配额/trace 那几节。"
)


async def collect_context(
    session: AsyncSession,
    interview_id: str,
    *,
    state: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """读业务库的行 + 图终态，拼成渲染要的三份数据。找不到报告返回 `None`（404 归调用方）。

    `state` 是注入口：脚本对**旧报告**（`thread_id` 为空，没法定位线程）自带一套扫全部线程
    的兜底，端点不许走那条路——那是一次 O(所有运行) 的读，而它在 HTTP 路径上。
    不给时按 `interview.thread_id` 读那一个线程；那一列是空串就读不到，
    于是渲染出来的是"只有题目"的报告（调用方负责把 `warning` 写成一句人话）。
    """
    head = await InterviewRepository(session).get_by_id(interview_id)
    if head is None:
        return None
    resume = await ResumeRepository(session).get_by_id(str(head.resume_id))
    job = await JobRepository(session).get_by_id(str(head.job_id)) if head.job_id else None
    rows = await QuestionRepository(session).list_for_interview(head.id)
    if state is None:
        state = await read_thread_state(head.thread_id)
    return {
        "head": _head_info(head, resume, job, state),
        "questions": _question_rows(rows, state),
        "state": dict(state),
        "warning": None,
    }


def _head_info(
    head: Interview,
    resume: Resume | None,
    job: Job | None,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    """报告头 + 它两侧的源表快照。源表可能已经改名或下线，所以取不到就回落，不报错。

    姓名回落看图终态：`scripts/run_e2e_live.py` 那条链路不把解析结果写回 `resume` 行
    （回写是 `tasks/resume_parsing.py` 的职责），所以那份报告的 `resume.name` 是空的，
    而标题不该因此变成"候选人"。
    """
    parsed_name = str((state.get("parsed") or {}).get("name") or "")
    return {
        "id": head.id,
        "status": head.status,
        "review_issues": list(head.review_issues or []),
        "created_at": str(head.created_at)[:19],
        "name": (resume.name if resume else "") or parsed_name or "候选人",
        "file_name": resume.file_name if resume else "?",
        "job_title": job.title if job else "未知岗位",
        "jd_text": job.jd_text if job else "",
    }


def _question_rows(rows: Sequence[Question], state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """题目行。`topic` 不在 `question` 表里（落库前被 `ROW_COLUMNS` 摘除），只能按工序号
    从图终态对回来——图终态缺席时是空串，渲染成"—"而不是编一个主题名。"""
    by_order = {int(item.get("order_index") or 0): item for item in state.get("questions") or []}
    out: list[dict[str, Any]] = []
    for offset, row in enumerate(rows):
        out.append(
            {
                "stem": row.stem,
                "difficulty": row.difficulty,
                "source": row.source,
                "reference_answer": row.reference_answer,
                "checkpoints": list(row.checkpoints or []),
                "rubric": list(row.rubric or []),
                "follow_ups": list(row.follow_ups or []),
                "evidence": row.evidence,
                "topic": str(by_order.get(offset, {}).get("topic") or "—"),
            }
        )
    return out


def _cell(value: Any) -> str:
    """表格单元：竖线与换行会撕开 Markdown 表格，先压平。"""
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _bullets(lines: list[str], items: Sequence[Any]) -> None:
    if items:
        lines.extend(f"- {item}" for item in items)
    else:
        lines.append("- （无）")


def _joined(items: Sequence[Any], render: Any) -> str:
    return "；".join(render(item) for item in items) or "（无）"


def render_markdown(context: Mapping[str, Any]) -> str:
    """把 `collect_context` 的结果渲染成一份报告。纯函数：不读库、不调模型。"""
    head: dict[str, Any] = context["head"]
    questions: list[dict[str, Any]] = context["questions"]
    state: Mapping[str, Any] = context["state"]
    profile: Mapping[str, Any] = state.get("profile") or {}
    plan: Mapping[str, Any] = state.get("plan") or {}
    parsed: Mapping[str, Any] = state.get("parsed") or {}
    lines: list[str] = []
    add = lines.append

    add(f"# 面试题报告 · {head['name']}（{head['job_title']}）")
    add("")
    add(
        f"> 由 InterviewForge 的七节点 LangGraph 工作流（parse → validate → match → profile "
        f"→ plan → generate → review）生成并落库。报告 id `{head['id']}`｜简历 "
        f"`{head['file_name']}`｜状态 **{head['status']}**｜生成时间 {head['created_at']}。"
    )
    add("")
    if context.get("warning"):
        add(f"> **注意**：{context['warning']}")
        add("")

    add("## 1. 简历分析")
    add("")
    add(
        f"- **候选人**：{parsed.get('name') or head['name']}"
        f"｜{parsed.get('years_experience')} 年经验"
        f"｜{parsed.get('phone') or '电话未在简历里认出'}"
    )
    educations = parsed.get("educations") or []
    works = parsed.get("work_experiences") or []
    projects = parsed.get("projects") or []
    skills = parsed.get("skills") or []
    add(
        "- **教育**："
        + _cell(
            _joined(
                educations,
                lambda e: "{} · {} · {}（{}–{}）".format(
                    e.get("school"),
                    e.get("major"),
                    DEGREE_CN.get(str(e.get("degree")), e.get("degree")),
                    e.get("start_year"),
                    e.get("graduate_year"),
                ),
            )
        )
    )
    add(
        "- **工作**："
        + _cell(
            _joined(
                works,
                lambda w: "{}｜{}（{}–{}）".format(
                    w.get("company"), w.get("role"), w.get("start"), w.get("end")
                ),
            )
        )
    )
    skill_names = ", ".join(str(s.get("name")) for s in skills) or "（无）"
    add(f"- **技能（{len(skills)} 项）**：{skill_names}")
    add(
        "- **项目**："
        + _cell(
            _joined(
                projects,
                lambda p: f"{p.get('name')}（{len(p.get('highlights') or [])} 条成果）",
            )
        )
    )
    add(f"- **岗位要求（JD）**：{_cell(head['jd_text'])}")
    add("")

    if profile:
        add(
            f"**画像综述**（五维由代码打分 `rubric {profile.get('rubric_version')}`，"
            f"叙述由模型写、`confidence={profile.get('confidence')}`）："
        )
        add("")
        add(f"> {profile.get('summary')}")
        add("")
        add("**优势**")
        add("")
        _bullets(lines, list(profile.get("strengths") or []))
        add("")
        add("**风险与追问点**")
        add("")
        _bullets(lines, list(profile.get("weaknesses") or []))
        add("")

        add("### 1.1 五维评分")
        add("")
        add("| 维度 | 分数 | 判据 | 关键信号 |")
        add("|---|---|---|---|")
        for dim in profile.get("dimensions") or []:
            signals = ", ".join(f"{k}={v}" for k, v in (dim.get("signals") or {}).items())
            add(
                f"| {_cell(dim.get('label'))} | **{dim.get('score')}** "
                f"| {_cell(dim.get('evidence'))} | {_cell(signals)} |"
            )
        add("")

        add("### 1.2 优先考察主题（画像薄弱项 → 出题方向）")
        add("")
        add("| 主题 | 关联维度 | 分数 | 为什么 |")
        add("|---|---|---|---|")
        dim_labels = {
            str(dim.get("name")): str(dim.get("label")) for dim in profile.get("dimensions") or []
        }
        for topic in profile.get("focus_topics") or []:
            dimension = str(topic.get("dimension"))
            add(
                f"| {_cell(topic.get('topic'))} | {dim_labels.get(dimension, dimension)} "
                f"| {topic.get('score')} | {_cell(topic.get('why'))} |"
            )
        add("")

    gaps = list(state.get("matched_gaps") or [])
    if gaps:
        add("### 1.3 JD 要求逐条判定")
        add("")
        add("| JD 要求 | 判定 | 命中技能 | 判据来源 | 简历证据 |")
        add("|---|---|---|---|---|")
        for gap in gaps:
            level = str(gap.get("level"))
            add(
                f"| {_cell(gap.get('requirement'))} | {LEVEL_CN.get(level, level)} "
                f"| {_cell(gap.get('skill'))} | {_cell(gap.get('source'))} "
                f"| {_cell(gap.get('evidence'))} |"
            )
        add("")

    add("## 2. 出题配额表（`plan` 节点的算术结果）")
    add("")
    quotas = ", ".join(
        f"{DIFFICULTY_CN.get(key, key)} {value} 道"
        for key, value in (plan.get("difficulty_quotas") or {}).items()
    )
    add(
        f"- 目标题数 **{plan.get('total')}**｜难度配比：{quotas}｜"
        f"策略 `{plan.get('strategy')}`（先问薄弱项）｜主题数 {plan.get('topic_count')}"
    )
    add("")
    add("| 主题 | 难度 | 题数 | 出题依据 | 为什么问它 |")
    add("|---|---|---|---|---|")
    for item in plan.get("items") or []:
        add(
            f"| {_cell(item.get('topic'))} "
            f"| {DIFFICULTY_CN.get(str(item.get('difficulty')), item.get('difficulty'))} "
            f"| {item.get('count')} "
            f"| {SOURCE_CN.get(str(item.get('source')), item.get('source'))} "
            f"| {_cell(item.get('rationale'))} |"
        )
    add("")

    add(f"## 3. 面试题（{len(questions)} 道）")
    add("")
    add("每题给：题干 → 参考答案 → 评分要点 → 打分标准 → 追问 → 可回溯依据。")
    for index, question in enumerate(questions, start=1):
        difficulty = DIFFICULTY_CN.get(question["difficulty"], question["difficulty"])
        source = SOURCE_CN.get(question["source"], question["source"])
        add("")
        add(f"### 第 {index} 题 · {difficulty} · {source}｜{question['topic']}")
        add("")
        add(f"**题干**：{question['stem']}")
        add("")
        add(f"**参考答案**：{question['reference_answer']}")
        add("")
        add("**评分要点**")
        add("")
        _bullets(lines, question["checkpoints"])
        add("")
        add("**打分标准**")
        add("")
        _bullets(lines, question["rubric"])
        add("")
        add("**追问**")
        add("")
        _bullets(lines, question["follow_ups"])
        add("")
        if question["evidence"]:
            add(f"**依据出处**：{question['evidence']}")
        else:
            add("**依据出处**：通用题，不绑定原文")

    add("")
    add("## 4. 评审批注（`review` 节点对这批量题的意见）")
    add("")
    issues = list(head["review_issues"] or [])
    if issues:
        add("| 涉及题号 | 提出方 | 问题 | 建议改法 |")
        add("|---|---|---|---|")
        for issue in issues:
            raw_index = issue.get("index")
            where = "整批" if raw_index is None else f"第 {int(raw_index) + 1} 题"
            add(
                f"| {where} | {_cell(issue.get('origin'))} | {_cell(issue.get('problem'))} "
                f"| {_cell(issue.get('fix'))} |"
            )
    else:
        add("- （无：评审没挑出问题）")
    add("")
    reflect = list(state.get("reflect_issues") or [])
    add(
        f"- 批注不拦门（ADR-0015）：上面 {len(issues)} 条只标出该改的题，"
        f"题目全部照常落库；`validate` 回退 {state.get('validate_retries', 0)} 次"
        f"（规则问题才回退；反思另有 {len(reflect)} 条，只记录不重跑——ADR-0022）。"
    )

    add("")
    add("## 5. 运行观测")
    add("")
    trace = list(state.get("trace") or [])
    if trace:
        add("| 节点 | 耗时 | 模板版本 | 结果 |")
        add("|---|---|---|---|")
        for step in trace:
            ms = float(step.get("ms") or 0)
            mark = "ok" if step.get("ok") else "failed"
            add(
                f"| {_cell(step.get('node'))} | {ms:.0f} ms "
                f"| {_cell(step.get('prompt_version'))} | {mark} |"
            )
        add("")
    add(
        f"- 带可回溯引用比例：**{evidence_ratio(questions):.0%}**（验收线 ≥ 90%）"
        f"｜题数 {len(questions)} / 计划 {plan.get('total')}"
    )
    add("- 落库判据：`interview.status = completed` 且 `question` 行数 == `total_questions`")
    add("- 上面这些数字都来自这一次运行的真实记录，不是模板样例。")
    add("")
    add("---")
    add("")
    add("重跑一份新报告（需要 DeepSeek 账户有余额；第 2 个参数可换 JD）：")
    add("")
    add("```bash")
    add("PYTHONPATH=. .venv/Scripts/python.exe -X utf8 scripts/run_e2e_live.py \\")
    add('    "docs/附件简历-梁清生.pdf" "<岗位 JD 文本>"')
    add("```")
    add("")
    return "\n".join(lines)
