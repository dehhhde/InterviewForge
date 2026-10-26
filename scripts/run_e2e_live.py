"""M5 的端到端验收脚本：一份真简历 -> 15 道题 -> 落库（**会真花钱**，不在 pytest 里）。

跑法（仓库根，需要 `.env` 里的 DeepSeek key）：

    PYTHONPATH=. .venv/Scripts/python.exe -X utf8 scripts/run_e2e_live.py \
        "tests/fixtures/resumes/附件简历-梁清生.pdf"

计划书的 M5 验收两条都在这里出数：题目数 == `settings.question.total`、
带可回溯引用的比例 >= 0.9（`question_service.evidence_ratio` 的口径），
外加 `trace` 打印——每步用了哪版 prompt 模板、花了多少毫秒、有没有回退。

为什么是一个脚本而不是一个测试：它要联网、要花钱、结果每年会漂。AGENTS.md §6 把
"断言模型文案"的用例明确排除在测试之外，这里连文案都不看，只看结构性的数；
但它仍然不该进 `pytest`——离线测试套件红了要能立刻知道是我们的代码坏了，而不是网络坏了。

JD 默认取库里第一个岗位（先跑 `python -m app.scripts.seed`），这样验收用的 JD 与
`seed` 的样本岗位是同一份，不额外发明第二套真相。要换 JD 就传第二个参数：一个文本文件路径。
"""

import asyncio
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.workflow import (  # noqa: E402
    build_agent_graph,
    make_thread_config,
    open_checkpointer,
)
from app.config import get_settings  # noqa: E402
from app.dependencies import get_sessionmaker  # noqa: E402
from app.repositories.interview_repository import InterviewRepository  # noqa: E402
from app.repositories.job_repository import JobRepository  # noqa: E402
from app.repositories.question_repository import QuestionRepository  # noqa: E402
from app.repositories.resume_repository import ResumeRepository  # noqa: E402
from app.services.question_service import evidence_ratio  # noqa: E402
from app.tasks.question_generation import make_live_nodes  # noqa: E402
from app.utils.db import session_scope  # noqa: E402
from app.utils.llm_client import LLMClient  # noqa: E402


def _stage_pdf(pdf: Path, resume_id: str) -> str:
    """把 PDF 复制成 `upload_dir/{resume_id}.pdf`，返回落盘相对路径。

    `parse` 节点只认 `locate_resume_file` 的命名约定，不接受任意路径，所以"上传"这步
    必须在跑图之前完成。文件 IO 放在同步函数里：脚本没有并发，别让 async 函数抱着
    阻塞调用占着事件循环（ruff ASYNC240）。
    """
    settings = get_settings()
    stored = f"{resume_id}.pdf"
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    target = settings.upload_dir / stored
    target.write_bytes(pdf.read_bytes())
    return stored


async def _seed_rows(pdf: Path, resume_id: str, jd_text: str) -> str:
    """落 `resume` + `interview(running)` 两行，返回报告头 id。"""
    stored = _stage_pdf(pdf, resume_id)

    async with session_scope(get_sessionmaker()) as session:
        resume = await ResumeRepository(session).create(
            id=resume_id, stored_path=stored, file_name=pdf.name
        )
        jobs = await JobRepository(session).list()
        job = next((item for item in jobs if item.jd_text == jd_text), jobs[0] if jobs else None)
        head = await InterviewRepository(session).create(
            resume_id=resume.id, job_id=job.id if job else None, plan={}, status="running"
        )
    return str(head.id)


def _jd_from_arg(explicit: str) -> str | None:
    path = Path(explicit)
    return path.read_text(encoding="utf-8") if path.is_file() else explicit


async def _jd_text(explicit: str | None) -> str:
    if explicit:
        return _jd_from_arg(explicit)
    async with session_scope(get_sessionmaker()) as session:
        jobs = await JobRepository(session).list()
    if not jobs:
        raise SystemExit("库里没有岗位，先跑 `python -m app.scripts.seed`")
    return str(jobs[0].jd_text)


def _report(result: dict[str, Any], client: LLMClient) -> None:
    settings = get_settings()
    questions = result.get("questions") or []
    ratio = evidence_ratio(questions)
    issues = result.get("review_issues") or []

    print("\n--- trace ---")
    for step in result.get("trace") or []:
        version = step.get("prompt_version", "-")
        print(f"{step['node']:<13} {step['ms']:>8.1f} ms  ok={step['ok']}  prompt={version}")
    print("\n--- errors（规则与降级流水账）---")
    for note in result.get("errors") or []:
        print(f"- {note}")
    print("\n--- 配额表 ---")
    for item in (result.get("plan") or {}).get("items") or []:
        print(f"- {item.get('topic')} / {item.get('difficulty')} × {item.get('count')}")
    print("\n--- 题目 ---")
    for index, question in enumerate(questions, start=1):
        mark = "✓" if question.get("evidence") else "·"
        print(f"{index:>2}. [{mark}] {question['difficulty']:<6} {question['stem'][:48]}")
    # 批注要连着题号一起打出来：ADR-0015 之后评审不拦门，"哪几题要改"就是它唯一的产出
    if issues:
        print("\n--- 评审批注 ---")
        for issue in issues:
            where = "整批" if issue.get("index") is None else f"第 {issue['index'] + 1} 题"
            print(f"- [{issue.get('origin')}] {where}：{str(issue.get('problem'))[:80]}")

    print("\n--- 汇总 ---")
    print(
        f"模型调用：{client.usage.calls} 次，"
        f"prompt {client.usage.prompt_tokens} / completion {client.usage.completion_tokens} tokens"
    )
    print(f"题目数：{len(questions)}（期望 {settings.question.total}）")
    print(f"带可回溯引用比例：{ratio:.2%}（验收线 >= 90%）")
    flagged = sum(1 for issue in issues if issue.get("index") is not None)
    print(f"评审批注：{len(issues)} 条（点名 {flagged} 题，整批 {len(issues) - flagged} 条）")
    print(f"finished={result.get('finished')}  批次={result.get('question_batch')}")
    print(
        "配额总数 == 期望题数："
        f"{sum(int(i.get('count') or 0) for i in (result.get('plan') or {}).get('items') or [])}"
        f" == {settings.question.total}"
    )


async def _read_back(interview_id: str) -> tuple[str, int, int]:
    async with get_sessionmaker()() as session:
        head = await InterviewRepository(session).get_by_id(interview_id)
        rows = await QuestionRepository(session).list_for_interview(interview_id)
        return (str(head.status) if head else "missing"), (
            int(head.total_questions) if head else -1
        ), len(rows)


def _resolve_pdf_arg() -> Path:
    """命令行校验放在同步函数里：`is_file()` 是阻塞 IO，不该出现在 async 函数体内。"""
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    pdf = Path(sys.argv[1])
    if not pdf.is_file():
        raise SystemExit(f"找不到简历文件：{pdf}")
    return pdf


async def main() -> int:
    pdf = _resolve_pdf_arg()
    jd_text = await _jd_text(sys.argv[2] if len(sys.argv) > 2 else None)

    client = LLMClient()
    resume_id = uuid.uuid4().hex
    # 报告头得在跑之前就有 id：`review` 收尾按 `interview_id` 落库，而它只能从 state 拿
    interview_id = await _seed_rows(pdf, resume_id, jd_text)
    print(f"简历：{pdf.name}  resume_id={resume_id}  interview_id={interview_id}")
    print(f"JD：{jd_text[:60]!r}…")

    async with open_checkpointer() as saver:
        graph = build_agent_graph(
            make_live_nodes(client, get_sessionmaker()), checkpointer=saver
        )
        result = await graph.ainvoke(
            {"resume_id": resume_id, "interview_id": interview_id, "jd_text": jd_text},
            make_thread_config(),
        )

    _report(result, client)
    status, total, rows = await _read_back(interview_id)
    print(f"落库：status={status} total_questions={total} question 行={rows}")

    settings = get_settings()
    questions = result.get("questions") or []
    ok = (
        bool(result.get("finished"))
        and len(questions) == settings.question.total
        and evidence_ratio(questions) >= 0.9
        and status == "completed"
        and rows == len(questions)
    )
    print(f"\n验收：{'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
