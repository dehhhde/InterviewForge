"""审查的规则层、模型批注收口、报告落库〔阶段 7 · §10.4.7，阶段 9 幻觉检测〕

分工还是 ADR-0009 那条线：**跨题才判得了的机械事实走规则，单题的语义硬伤走模型**。

- 规则层（`rule_issues`）：题干字面重复、每题的引用能否逐字回溯到它自己那份原文（简历或
  JD，判据与出题闸门同为 `evidence_problem`）、这批题距 `plan.items` 的计划总数差几题。
  这三件事的共同点是"必须看见整批"——出题分支各自只拿到一条 `plan.items` 槽位，跨槽位的
  重复和总数缺口在分支里无从判断，只能在汇合后的 `review` 补一刀。`generate` 侧那道闸门管的
  是"本槽位内"，两者不重复。
- 模型层（`review_node` 调 `llm_model_review` 那个独立槽位）：证据撑不撑得起题干、能否作答、
  能否评分、难度名不副实、换皮重复。这些没有机械判据。计划书 §6 风险 6 要的"出题与评审
  不复用同一次思考"靠**独立模型槽位 + 不传生成时的推理**落地：这里只把题目本身和简历
  抽取结果交给评审。

`review_issues` 里每条都带 `origin`（`rule` / `model`），原样落进 `interview.review_issues`。
归因很有用：规则类批注复现说明毛病在配额表或 prompt 的硬规则上，模型类批注复现则指向出题
模板本身——两边指向的修法不一样。

ADR-0015 起 `review` **不再拦门**：批注只标注哪道题有问题，题目一律照常落库，
所以本文件里已经没有"占比""阈值""要不要重做"这类判据函数。

`persist_report` 是本文件唯一碰数据库的函数，也是整条流水线唯一的写库点（AGENTS.md §3：
节点不直连数据库）。它把 `interview` + N 行 `question` 放进**一个事务**：半份报告比没有
报告更糟——读的人分不清它是坏了还是还没跑完。

阶段 9 的幻觉检测（计划书 §11）不用另起一套判据：它要的"这句话在原文里逐字找不找得到"
就是 `rule_issues` 里那条 `evidence_belongs`，扩的是覆盖范围而不是机制。
"""

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.extractors.base import collapse
from app.repositories.interview_repository import InterviewRepository
from app.repositories.question_repository import QuestionRepository
from app.services.question_service import (
    evidence_problem,
    order_questions,
    planned_count,
    to_row,
)
from app.utils.db import session_scope

#: `origin` 的两取值：规则层 / 模型层。落库后靠它做归因，别在这里发明第三种。
RULE_ORIGIN = "rule"
MODEL_ORIGIN = "model"

#: 单条批注的字数上限。模型偶尔会写一段小作文，而 `interview.review_issues` 整份进 JSON 列，
#: 不设上限就等于把一次评审的完整输出留在报告头里
MAX_ISSUE_CHARS = 300


class _ModelIssue(BaseModel):
    """模型对单道题的批注形状。

    刻意写得比最终落库的形状松（`index` 允许负数、`problem` 允许空）：越界的序号和空话
    由 `adopt_model_issues` 挡掉并记账，而不是让 `chat_json` 拿"输入不是合法整数"去反复重问。
    """

    index: int = -1
    problem: str = ""
    fix: str = ""


class ReviewPayload(BaseModel):
    """`review.system.j2` 约定的返回形状。"""

    issues: list[_ModelIssue] = Field(default_factory=list)


def make_issue(
    problem: str,
    *,
    index: int | None = None,
    fix: str = "",
    origin: str = RULE_ORIGIN,
) -> dict[str, Any]:
    """造一条批注。`index=None` 表示"这批题整体有问题"，不指向某一题。"""
    issue: dict[str, Any] = {
        "index": index,
        "problem": collapse(problem)[:MAX_ISSUE_CHARS],
        "fix": collapse(fix)[:MAX_ISSUE_CHARS],
        "origin": origin,
    }
    return issue


def rule_issues(
    questions: Sequence[Mapping[str, Any]],
    *,
    raw_text: str,
    plan: Mapping[str, Any] | None,
    jd_text: str = "",
) -> list[dict[str, Any]]:
    """整批的机械检查。返回按报告题号排序后的批注列表。

    题号口径以 `order_questions` 为准：分支返回的先后是调度决定的，而批注里的 `index`
    要指得准题目，必须先排定同一份顺序。
    """
    ordered = order_questions(questions)
    issues: list[dict[str, Any]] = []

    seen: dict[str, int] = {}
    for index, question in enumerate(ordered):
        stem = collapse(str(question.get("stem") or ""))
        first = seen.get(stem)
        if first is not None:
            issues.append(
                make_issue(
                    f"第 {index + 1} 题与第 {first + 1} 题题干重复",
                    index=index,
                    fix="换考察点重出，不要改措辞",
                )
            )
        else:
            seen[stem] = index

        # 与出题闸门同一个判据（`evidence_problem`）：这里再核一遍是防"外部塞进来的题目"
        # 和分支逻辑回归，不是第二套标准
        reason = evidence_problem(
            str(question.get("source") or "generic"),
            collapse(str(question.get("evidence") or "")),
            raw_text=raw_text,
            jd_text=jd_text,
        )
        if reason is not None:
            issues.append(
                make_issue(
                    f"第 {index + 1} 题的引用回指不到原文：{reason[:60]}",
                    index=index,
                    fix="补上逐字出自对应原文的 evidence，否则把 source 改成 generic",
                )
            )

    issues += _shortfall_issues(ordered, plan)
    return issues


def _shortfall_issues(
    ordered: Sequence[Mapping[str, Any]], plan: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    """距计划总数缺几题就记几条批注。

    为什么一题一条而不是一条"缺 3 题"：批注的读者是页面上逐题看卷子的人，一条批注对应
    "报告里少了一道题"这个事实，合并成一条会让人以为只缺一题。缺题在 `plan` 与扇出的
    `Send` 数量一致时不该发生，真发生了就是出题分支整体失败，批注要吵得足够响。
    """
    items = _plan_items(plan)
    planned = planned_count(items)
    missing = planned - len(ordered)
    return [
        make_issue(
            f"计划 {planned} 题，到手 {len(ordered)} 题：第 {len(ordered) + offset + 1} 题缺失",
            fix="按未满足的槽位主题补出题，不要重做已合格的题目",
        )
        for offset in range(max(0, missing))
    ]


def adopt_model_issues(
    payload: ReviewPayload, *, total: int
) -> tuple[list[dict[str, Any]], list[str]]:
    """收口模型给的批注。返回（合格批注，给人看的说明）。

    闸门只有两条，都在"这条批注指不指得准题目"上：序号越界、正文是空话。
    **不判模型说的对不对**——难度名不副实这类语义问题没有机械判据，这里替模型否决
    就等于用规则重写了评审标准。
    """
    issues: list[dict[str, Any]] = []
    notes: list[str] = []
    seen: set[tuple[int, str]] = set()
    for raw in payload.issues:
        problem = collapse(raw.problem)
        if not problem:
            notes.append("模型给了一条没有正文的批注，已丢弃")
            continue
        index = int(raw.index)
        if not 0 <= index < total:
            notes.append(f"模型报的序号 {index} 超出题目范围（共 {total} 题），已丢弃")
            continue
        key = (index, problem)
        if key in seen:
            notes.append(f"模型对第 {index + 1} 题重复报了同一条批注，已合并")
            continue
        seen.add(key)
        issues.append(make_issue(problem, index=index, fix=raw.fix, origin=MODEL_ORIGIN))
    return issues, notes


async def persist_report(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    interview_id: str,
    resume_id: str,
    questions: Sequence[Mapping[str, Any]],
    batch: int,
    plan: Mapping[str, Any] | None,
    review_issues: Sequence[Mapping[str, Any]],
) -> int:
    """把一份报告落库（题目 + 报告头 + 批注，一个事务），返回写入的题目行数。

    ADR-0015：状态恒为 `completed`。批注只说明"哪几题有问题"，不再决定"这份卷子能不能要"，
    所以这里没有"判死就不写题"的分支了。`aborted` 只剩一条来路——后台任务跑到一半崩了，
    由 `tasks/question_generation._abort_interview` 补刀。

    幂等（AGENTS.md §4.6）靠 `question` 表的 `(interview_id, batch, order_index)` 唯一约束
    兜底，但更靠前置检查：`latest_batch(interview_id)` 已经等于本批就说明写过了，直接跳过
    写题只改状态。同一线程被重放时会给出**新批次**，"新批次 + 旧最大批次"永远不等，
    不会把该写的题漏掉。

    `interview` 行正常由调用方（API / 后台任务）在开跑前建成 `running`；这里兼容
    "调用方没建行"的情形补一行，因为脚本与测试也走这条出口。补行要求 `resume_id`
    在 `resume` 表里真实存在——外键是开的，编造的 id 会让整个事务回滚，
    这比静默写出一份孤儿报告好得多。
    """
    ordered = order_questions(questions)
    async with session_scope(sessionmaker) as session:
        interviews = InterviewRepository(session)
        head = await interviews.get_by_id(interview_id)
        if head is None:
            head = await interviews.create(
                id=interview_id,
                resume_id=resume_id,
                plan=dict(plan or {}),
                review_issues=[dict(issue) for issue in review_issues],
                status="running",
            )
        repo = QuestionRepository(session)
        if ordered and await repo.latest_batch(interview_id) != batch:
            await repo.add_many(
                [to_row(question) for question in ordered],
                interview_id=interview_id,
                batch=batch,
            )
        await interviews.update(
            head.id,
            total_questions=len(ordered),
            review_issues=[dict(issue) for issue in review_issues],
        )
        await interviews.set_status(head.id, "completed")
    return len(ordered)


def _plan_items(plan: Any) -> list[Mapping[str, Any]]:
    if not isinstance(plan, Mapping):
        return []
    items = plan.get("items")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return []
    return [item for item in items if isinstance(item, Mapping)]
