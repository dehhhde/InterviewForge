"""把一份报告导成 Markdown：题目读业务库，分析过程读 checkpoint〔不花钱，离线可跑〕

用法（仓库根）：

    PYTHONPATH=. .venv/Scripts/python.exe -X utf8 scripts/export_report_md.py \
        <interview_id 或前缀> [输出.md]

为什么读**两处**：题目与报告头以业务库为准——那是 `review` 收尾写下的终态
（ADR-0015 起评审只批注、不拦门，题目一律落库），而画像五维、JD 缺口、配额表、trace 这些"分析过程"
只在 checkpoint 的图终态里：`interview.plan` 落的是空对象，报告头只有
`status` / `total_questions` / `review_issues`。所以脚本用业务库的行保证"面试官看到的就是
交付的那 15 题"，再用图终态补齐"这些题是怎么被分配出来的"。`question` 表没有 `topic` 列，
主题同样只能从图终态按 `order_index` 对回来。

线程 id 没进业务库（`make_thread_config()` 每次随机、跑完不回填），所以只能扫全部线程再按
`interview_id` 认。扫描走 `mode=ro`：checkpoint 库不是业务库，没有 repository 可走，
但也不能被一个导出脚本写坏。`StateGraph(AgentState)` 挂一个空节点就够 `aget_state` 用——
它读的是 channel 值，不需要真跑图（langgraph 要求图有入口点，那个空节点省不掉）。
"""

import asyncio
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from langgraph.graph import END, START, StateGraph  # noqa: E402

from app.agents.state import AgentState, NodeName  # noqa: E402
from app.agents.workflow import open_checkpointer  # noqa: E402
from app.dependencies import get_sessionmaker  # noqa: E402
from app.repositories.interview_repository import InterviewRepository  # noqa: E402
from app.repositories.job_repository import JobRepository  # noqa: E402
from app.repositories.question_repository import QuestionRepository  # noqa: E402
from app.repositories.resume_repository import ResumeRepository  # noqa: E402
from app.services.question_service import evidence_ratio  # noqa: E402
from app.utils.db import session_scope  # noqa: E402

DIFFICULTY_CN = {"easy": "简单", "medium": "中等", "hard": "较难"}
SOURCE_CN = {"resume": "简历", "jd": "JD", "generic": "通用"}
LEVEL_CN = {
    "matched": "命中",
    "partial": "部分命中",
    "missing": "无证据",
    "unverified": "待确认",
}
DEGREE_CN = {
    "college": "大专",
    "bachelor": "本科",
    "master": "硕士",
    "phd": "博士",
    "other": "其他",
}


def _threads() -> list[str]:
    """checkpoint 库里的全部线程 id（只读连接）。"""
    path = ROOT / "data" / "checkpoints.db"
    if not path.is_file():
        return []
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute("select distinct thread_id from checkpoints")
        return [str(row[0]) for row in rows]
    finally:
        connection.close()


async def _final_state(interview_id: str) -> dict[str, Any]:
    """按 `interview_id` 认出线程并取图终态；找不到返回空 dict。

    节点名要**全部登记**再连一条 `START → parse → END`：`generate` 的 pending sends 里存着
    `generate_item`，图里没有这个名字时 langgraph 会给每个分支打一行
    "Ignoring unknown node name"。我们只读 channel 值，不跑图，但日志得干净。
    """
    async with open_checkpointer() as saver:
        builder: StateGraph = StateGraph(AgentState)
        for name in NodeName:
            builder.add_node(str(name), lambda _state: {})
        builder.add_edge(START, str(NodeName.PARSE))
        builder.add_edge(str(NodeName.PARSE), END)
        graph = builder.compile(checkpointer=saver)
        for thread in _threads():
            snapshot = await graph.aget_state({"configurable": {"thread_id": thread}})
            values = dict(snapshot.values or {})
            if values.get("interview_id") == interview_id:
                return values
    return {}


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


def _md(context: dict[str, Any]) -> str:
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
        f"`{head['file_name']}`｜状态 **{head['status']}**｜批次 {head['batch']}｜"
        f"生成时间 {head['created_at']}。"
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

    add(f"## 3. 面试题（{len(questions)} 道 · 批次 {head['batch']}）")
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
    add(
        f"- 批注不拦门（ADR-0015）：上面 {len(issues)} 条只标出该改的题，"
        f"题目全部照常落库；`validate` 回退 {state.get('validate_retries', 0)} 次。"
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


async def _collect(interview_id: str) -> dict[str, Any]:
    """读业务库的行 + 图终态，拼成渲染要的三份数据。

    姓名以图终态的 `parsed.name` 兜底：`run_e2e_live.py` 这条链路不把解析结果写回 `resume`
    行（那是 `tasks/resume_parsing.py` 的职责），所以直接跑图的报告里 `resume.name` 是空的，
    而报告标题不该因此变成"候选人"。
    """
    state = await _final_state(interview_id)
    parsed_name = str((state.get("parsed") or {}).get("name") or "")
    async with session_scope(get_sessionmaker()) as session:
        head = await InterviewRepository(session).get_by_id(interview_id)
        if head is None:
            raise KeyError(f"报告不存在：{interview_id}")
        resume = await ResumeRepository(session).get_by_id(str(head.resume_id))
        job = await JobRepository(session).get_by_id(str(head.job_id)) if head.job_id else None
        rows = await QuestionRepository(session).list_for_interview(head.id)
        questions = [
            {
                "stem": row.stem,
                "difficulty": row.difficulty,
                "source": row.source,
                "reference_answer": row.reference_answer,
                "checkpoints": list(row.checkpoints or []),
                "rubric": list(row.rubric or []),
                "follow_ups": list(row.follow_ups or []),
                "evidence": row.evidence,
                "topic": "",
            }
            for row in rows
        ]
        info = {
            "id": head.id,
            "status": head.status,
            "review_issues": list(head.review_issues or []),
            "created_at": str(head.created_at)[:19],
            "name": (resume.name if resume else "") or parsed_name or "候选人",
            "file_name": resume.file_name if resume else "?",
            "job_title": job.title if job else "未知岗位",
            "jd_text": job.jd_text if job else "",
            "batch": max((row.batch for row in rows), default=0),
        }
    by_order = {int(item.get("order_index") or 0): item for item in state.get("questions") or []}
    for offset, question in enumerate(questions):
        question["topic"] = str(by_order.get(offset, {}).get("topic") or "—")
    return {"head": info, "questions": questions, "state": state, "warning": None}


async def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    async with session_scope(get_sessionmaker()) as session:
        rows = await InterviewRepository(session).list(limit=200)
    matches = [row.id for row in rows if row.id.startswith(sys.argv[1])]
    if not matches:
        print(f"库里找不到报告：{sys.argv[1]}")
        return 1
    if len(matches) > 1:
        print(f"前缀 {sys.argv[1]} 命中 {len(matches)} 份报告，请给完整 id")
        return 1

    context = await _collect(matches[0])
    if not context["state"]:
        context["warning"] = (
            "checkpoint 里找不到这份报告的图终态，所以只有题目，没有画像/配额/trace 那几节。"
        )
    elif context["head"]["file_name"].startswith("附件简历-梁清生"):
        context["warning"] = (
            "这批题是按**" + context["head"]["job_title"] + "**的 JD 生成的，而这份简历的主线是 "
            "Java 后端（Java / Spring Cloud / MySQL / Redis / 分布式）。当格式与质量样例看没问题，"
            "直接拿去面试会文不对题——换上后端 JD 重跑一次即可，命令在文末。"
        )

    text = _md(context)
    if len(sys.argv) > 2:
        out = Path(sys.argv[2])
    else:
        out = Path(f"reports/面试题-{context['head']['name']}-{context['head']['job_title']}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"已写出 {out.as_posix()}：{len(text)} 字，{len(context['questions'])} 道题")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
