"""POST /generate · GET /{id} · GET /list〔阶段 2 建壳，M6 接 BackgroundTasks〕

与 `resumes.py` 的上传同构：服务层建 `interview(running)` + `task(pending)` 并**当场提交**，
整张 LangGraph 交给 `BackgroundTasks` 后台跑，响应立刻带 `interview_id` + `task_id`。
注入会话工厂而不是请求会话的理由见 `resumes.py` 顶部（ADR-0011）。
出题进度（每个 super-step 一条）写进 `task.progress`，题目落库只在 `review` 收尾——
`GET /{id}` 在跑完前读到的就是 `status=running` 加空题目，这正是轮询方要的语义。
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks
from fastapi.responses import PlainTextResponse

from app.dependencies import KeywordQuery, PageLimit, PageOffset, SessionDep, SessionMakerDep
from app.exceptions.base import NotFoundError
from app.models.interview import Interview
from app.models.question import Question
from app.schemas.common import Page
from app.schemas.interview import (
    GenerateRequest,
    InterviewHeadFields,
    InterviewListItem,
    InterviewOut,
    InterviewStartOut,
    ReportQuestion,
)
from app.services import export_service, interview_service
from app.tasks.question_generation import run_question_generation

router = APIRouter(prefix="/interviews", tags=["interviews"])

#: 报告 DTO 里题目行的字段来自 `question` 表的这些列，`batch` 等工序列不外泄。
_QUESTION_FIELDS = (
    "order_index",
    "stem",
    "difficulty",
    "source",
    "evidence",
    "checkpoints",
    "reference_answer",
    "rubric",
    "follow_ups",
)


@router.post("/generate", status_code=202, response_model=InterviewStartOut)
async def generate_questions(
    background_tasks: BackgroundTasks, sessionmaker: SessionMakerDep, body: GenerateRequest
) -> InterviewStartOut:
    head, task, jd_text = await interview_service.start_generation(
        sessionmaker, resume_id=body.resume_id, job_id=body.job_id
    )
    background_tasks.add_task(
        run_question_generation,
        task.id,
        body.resume_id,
        head.id,
        jd_text,
        thread_id=head.thread_id,
        auto_fix=body.auto_fix,
    )
    return InterviewStartOut(interview_id=head.id, task_id=task.id)


# `/list` 的注册顺序约束见 `resumes.py` 的注释。
@router.get("/list", response_model=Page[InterviewListItem])
async def list_interviews(
    session: SessionDep,
    limit: PageLimit = 50,
    offset: PageOffset = 0,
    q: KeywordQuery = "",
) -> Page[InterviewListItem]:
    """一页报告头 + 满足同一条件的总数〔T-303 外壳 / T-301 投影〕。

    搜索打在报告自己的四列快照上（`interview_service.list_reports`）。题目行不在这一页里，
    而且**不是"空数组"**——列表行压根没这个字段，`aborted` 的"确实没题"与"列表不发正文"
    因此不会共用同一个形状。逐题正文只在 `GET /{id}` 给。
    """
    rows, total = await interview_service.list_reports(session, limit=limit, offset=offset, q=q)
    return Page(items=[_to_list_item(head) for head in rows], total=total)


@router.get("/{interview_id}", response_model=InterviewOut)
async def get_interview(interview_id: str, session: SessionDep) -> InterviewOut:
    report = await interview_service.fetch_report(session, interview_id)
    if report is None:
        raise NotFoundError("报告不存在", details={"interview_id": interview_id})
    head, questions = report
    return _to_out(head, questions)


@router.get("/{interview_id}/export.md")
async def export_interview_markdown(
    interview_id: str, session: SessionDep
) -> PlainTextResponse:
    """把一份报告导成 Markdown 供控制台下载〔T-302 / BACKLOG P3-10〕。

    纯读端点，所以用 `SessionDep`（AGENTS.md §3：会唤起后台任务的写端点才换 `SessionMakerDep`）。
    渲染出处只有一个（`export_service`），脚本 `scripts/export_report_md.py` 调同一个函数——
    这条端点存在的意义就是"两条路给同一份内容"，前端再自己拼一套就被它作废。

    **它不扫 checkpoint 线程表**：图终态按 `interview.thread_id` 直接定位（T-304）。
    旧报告那一列是空串，于是那几节整段不渲染并在文首留一句说明；让 HTTP 路径去做
    一次 O(所有运行) 的扫描是脚本该干的事，不是端点的。
    """
    context = await export_service.collect_context(session, interview_id)
    if context is None:
        raise NotFoundError("报告不存在", details={"interview_id": interview_id})
    if not context["state"]:
        context = {**context, "warning": export_service.NO_STATE_NOTE}
    text = export_service.render_markdown(context)
    head = context["head"]
    disposition = _attachment(f"面试题-{head['name']}-{head['job_title']}.md")
    return PlainTextResponse(
        text,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": disposition},
    )


def _attachment(file_name: str) -> str:
    """`Content-Disposition` 的值。文件名里是用户输入的简历名与岗位名，两个坑都得挡：

    - **换行**能直接把响应头切开（`\\r\\n` 后面跟着什么就是什么），所以先剥掉；
    - **中文与引号**放进 `filename="…"` 会破坏引号结构，所以走 RFC 5987 的
      `filename*=UTF-8''…`，并且 `safe=""`：`/` 与 `;` 也编掉，名字里带斜杠或分号
      同样拆不开这个头。同时留一份 ASCII 的 `filename=` 兜给不认 5987 的老客户端
      ——那是回退，不是真相。
    """
    clean = file_name.replace("\r", "").replace("\n", "").replace('"', "")
    return f"attachment; filename=\"report.md\"; filename*=UTF-8''{quote(clean, safe='')}"


def _head_values(head: Interview) -> dict[str, object]:
    """报告头 → 列表行与详情共用的那批值。

    键名取自 `InterviewHeadFields` 的字段表而不是在这里重列一遍：详情模型曾经把列表模型
    的十三个列整份抄走（T-301 要结束的就是这个形状），列名在这里再写一遍就成了第三份真相。
    `plan` / `review_issues` 是 ORM 加载出来的可变对象，复制一份再交给响应模型——
    序列化期间改到同一份 list 会写进已发出的载荷。
    """
    shared = InterviewHeadFields.model_fields
    values: dict[str, object] = {key: getattr(head, key) for key in shared}
    values["plan"] = dict(head.plan or {})
    values["review_issues"] = [dict(issue) for issue in head.review_issues or []]
    return values


def _to_list_item(head: Interview) -> InterviewListItem:
    return InterviewListItem(**_head_values(head))


def _to_out(head: Interview, questions: Sequence[Question]) -> InterviewOut:
    return InterviewOut(
        **_head_values(head),
        questions=[
            ReportQuestion(**{field: getattr(row, field) for field in _QUESTION_FIELDS})
            for row in questions
        ],
    )
