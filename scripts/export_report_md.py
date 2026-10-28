"""把一份报告导成 Markdown：命令行外壳，渲染与端点同一个出处〔阶段 9 建，S3 · T-302 改造〕

用法（仓库根）：

    PYTHONPATH=. .venv/Scripts/python.exe -X utf8 scripts/export_report_md.py \
        <interview_id 或前缀> [输出.md]

**渲染不在这里**。同一份文件的构造收进了 `app/services/export_service.py`，
`GET /interviews/{id}/export.md` 与本脚本调的是同一个函数——BACKLOG P3-10 记的
"两套分叉的 Markdown 导出，同名不同内容"到此为止：以后要改导出形状只有一个地方可改，
而"两条路内容一致"本身有测试钉着（`tests/test_api/test_export_api.py`）。

本脚本比端点多做的事只有两件，且都是命令行才该做的：

1. 接受 id **前缀**并检查唯一性（人打字不方便，端点不必供这个能力）；
2. 对**旧报告**兜底：`interview.thread_id` 是 T-304 新加的列，历史行是空串，
   那些线程当初没留名字，只能扫全部线程再按 `interview_id` 认回来。
   这是一次 O(所有运行) 的读，**端点明确不做**（不该出现在 HTTP 路径上），
   离线脚本才有资格做，而且只在报告确实没有可寻址线程时才做。
"""

import asyncio
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents import workflow  # noqa: E402
from app.agents.workflow import read_thread_state  # noqa: E402
from app.dependencies import get_sessionmaker  # noqa: E402
from app.repositories.interview_repository import InterviewRepository  # noqa: E402
from app.services import export_service  # noqa: E402
from app.utils.db import session_scope  # noqa: E402


def _thread_ids() -> list[str]:
    """checkpoint 库里的全部线程 id（只读连接）。

    路径**按模块属性取** `workflow.CHECKPOINT_DB`（不是 import 时抓一份值）：
    那正是 `read_thread_state` 默认打开的同一个库。两条路各自拼一遍
    `data/checkpoints.db`，早晚会有一天"清单里的线程去库里查不到"。
    走 `mode=ro`：checkpoint 库不是业务库，没有 repository 可走，但也不能被一个导出脚本写坏。
    """
    path = workflow.CHECKPOINT_DB
    if not path.is_file():
        return []
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute("select distinct thread_id from checkpoints")
        return [str(row[0]) for row in rows]
    finally:
        connection.close()


async def _state_by_scanning(interview_id: str) -> dict[str, Any]:
    """没有 `thread_id` 的旧报告：扫全部线程，按 `interview_id` 认出那一次运行。"""
    for thread in _thread_ids():
        values = await read_thread_state(thread)
        if values.get("interview_id") == interview_id:
            return values
    return {}


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
    interview_id = matches[0]

    async with session_scope(get_sessionmaker()) as session:
        head = await InterviewRepository(session).get_by_id(interview_id)
        thread_id = head.thread_id if head else ""
        # T-304 之前落库的报告没有可寻址的线程，只能扫；新报告直接按 id 读那一个线程。
        if thread_id:
            state = await read_thread_state(thread_id)
        else:
            state = await _state_by_scanning(interview_id)
        warning = export_service.NO_STATE_NOTE if not state else None
        context = await export_service.collect_context(session, interview_id, state=state)
    if context is None:
        print(f"库里找不到报告：{interview_id}")
        return 1
    if context["head"]["file_name"].startswith("附件简历-梁清生"):
        warning = (
            "这批题是按**" + context["head"]["job_title"] + "**的 JD 生成的，而这份简历的主线是 "
            "Java 后端（Java / Spring Cloud / MySQL / Redis / 分布式）。当格式与质量样例看没问题，"
            "直接拿去面试会文不对题——换上后端 JD 重跑一次即可，命令在文末。"
        )
    context = {**context, "warning": warning}

    text = export_service.render_markdown(context)
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
