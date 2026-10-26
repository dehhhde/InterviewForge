"""质量审查节点：规则先判整批，模型再逐题挑语义硬伤〔阶段 7 · §10.4.7〕

**ADR-0015 起这个节点不拦门**：它只产出"哪几题有问题"的批注，题目一律照常落库、
状态一律 `completed`。原来那道"批注占比超阈值就回退重做整批"的闸门连同
`route_after_review`、`review_retries`、`review_max_retries`、`review_fail_ratio_threshold`
一起下线了。下线理由不是"阈值调不准"，是**回退拿不到反馈**：`GenerateJob` 的载荷里没有
`review_issues`，重做等于用同一个 prompt 在同一批槽位上盲重掷，实测三轮的毛病同类复现，
只多烧两遍 token，最后把整份卷子判成 0 题。

节点契约（计划书 §4）在这里的落法：
- 只返回增量：`review_issues`（覆盖语义，每次评审重算整批）/ `finished`，
  `prompt_version` 走保留键交给 `traced()`；
- **可恢复失败**写 `errors`（评审端点挂了 → 题目照落，批注只到规则层，见下）；
- **不可恢复失败**只有一种：没有抽取结果可依据（抛 `ValidationFailedError`）。

为什么评审端点挂掉在这里被吞成一条流水账，而 `validate` 的反思环节同样吞掉：
两边理由相同——吞掉不会把"没人检查过"说成"检查过了"，因为批注列表本身就是证据，
页面按 `review_issues` 的条数打标，少一轮语义评审就少一批批注。但**必须留下话**，
所以吞掉的同时会补一条 `index=None` 的规则层批注，让人一眼看出这份卷子的语义检查没做完。

落库只有一次（`persist_report`），且节点自己不写 SQL：它只把装配期注入的会话工厂交给
service。
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.state import AgentState
from app.agents.workflow import PROMPT_VERSION_KEY, Node
from app.config import Settings, get_settings
from app.exceptions.base import LLMError, LLMResponseError, ValidationFailedError
from app.prompts.registry import Prompt, get_prompt
from app.services.question_service import dump, order_questions
from app.services.review_service import (
    ReviewPayload,
    adopt_model_issues,
    make_issue,
    persist_report,
    rule_issues,
)
from app.utils.llm_client import LLMClient

#: 评审是判定类调用，温度 0：同一批题两次评审应当给出同一份批注
_TEMPERATURE = 0.0

#: 交给评审的题目只带判据需要的字段。`batch` / `order_index` 是我们的工序信息，
#: `follow_ups` 不参与那五类语义判定——带进 prompt 只会白白多烧一遍 token
_REVIEW_FIELDS: tuple[str, ...] = (
    "stem",
    "difficulty",
    "source",
    "evidence",
    "checkpoints",
    "reference_answer",
    "rubric",
)


def make_review_node(
    *,
    llm: LLMClient | None = None,
    settings: Settings | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> Node:
    """造 review 节点。

    `sessionmaker` 缺省时收尾不落库（纯图测试与"只跑一次看看"的脚本用）；给了它但没有
    `interview_id`，同样跳过落库——那是"调用方没打算入库"，不是质量问题。
    """
    settings = settings or (llm.settings if llm is not None else get_settings())
    client = llm or LLMClient(settings)
    prompt = get_prompt("review")

    async def review_node(state: AgentState) -> dict[str, Any]:
        parsed = state.get("parsed")
        if not isinstance(parsed, dict):
            raise ValidationFailedError(
                "缺少简历抽取结果，评审无从比对",
                code="review_no_parsed",
                details={"resume_id": state.get("resume_id")},
            )

        questions = order_questions(state.get("questions") or [])
        raw_text = state.get("raw_text") or ""
        jd_text = state.get("jd_text") or ""
        errors: list[str] = []

        issues = rule_issues(
            questions,
            raw_text=raw_text,
            plan=state.get("plan"),
            jd_text=jd_text,
        )
        stamp = ""

        # 规则层的批注不再短路模型评审：这一轮是这批题唯一的一次语义检查，
        # 跳过就等于让被规则点名的批次连"能不能作答"都没人看过一眼。
        # 空批次例外——没有题可评，模型只会回一个空 issues。
        if not questions:
            errors.append("review: 本批一道题都没有，跳过语义评审")
        else:
            try:
                payload = await client.chat_json(
                    "review",
                    _messages(prompt, parsed, questions, jd_text),
                    schema=ReviewPayload,
                    temperature=_TEMPERATURE,
                )
            except (LLMError, LLMResponseError) as exc:
                # 吞掉但不静默：题目照落，语义这一层的缺口写成一条指不到具体题的批注，
                # 页面按批注条数打标，看报告的人知道这份卷子的检查只做了一半。
                exc.details["question_batch"] = state.get("question_batch", 0)
                issues.append(
                    make_issue(
                        f"语义评审这一轮没跑成（{exc.message}），上面的批注只覆盖规则层",
                        fix="需要完整评审就重新生成一份卷子",
                    )
                )
                errors.append(f"review: 评审调用失败，已按规则层批注落库 —— {exc.message}")
            else:
                adopted, notes = adopt_model_issues(payload, total=len(questions))
                issues = [*issues, *adopted]
                errors.extend(f"review: {note}" for note in notes)
                stamp = f"{prompt.node}@{prompt.version}"

        update: dict[str, Any] = {"review_issues": issues, "finished": True}
        if stamp:
            update[PROMPT_VERSION_KEY] = stamp
        errors.append(await _persist(state, questions, issues, sessionmaker))
        update["errors"] = errors
        return update

    return review_node


async def _persist(
    state: AgentState,
    questions: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    sessionmaker: async_sessionmaker[AsyncSession] | None,
) -> str:
    """落库收尾，并把结果写成一行流水账（返回值一定进 `errors`，不留空串）。

    批次取 `state["question_batch"]`：`generate` 每扇一次自增，落库的 `question.batch`
    与它同源。落库失败**不 catch**：题已经生成完了却没存下来属于事故，静默继续会产出一份
    "看起来跑完了但读不到结果"的报告。
    """
    interview_id = state.get("interview_id") or ""
    if sessionmaker is None or not interview_id:
        missing = "会话工厂" if sessionmaker is None else "interview_id"
        return f"review: 收尾未落库（缺 {missing}），{len(questions)} 道题只在 state 里"
    written = await persist_report(
        sessionmaker,
        interview_id=interview_id,
        resume_id=state.get("resume_id") or "",
        questions=questions,
        batch=int(state.get("question_batch") or 0),
        plan=state.get("plan"),
        review_issues=issues,
    )
    flagged = sum(1 for issue in issues if issue.get("index") is not None)
    return (
        f"review: 报告 {interview_id} 落库 {written} 题，"
        f"批注 {len(issues)} 条（点名 {flagged} 题）"
    )


def _messages(
    prompt: Prompt, parsed: dict[str, Any], questions: list[dict[str, Any]], jd_text: str
) -> list[dict[str, str]]:
    """渲染评审 prompt。

    给模型的 `index` 口径 = 这里数组的下标，与 `rule_issues` 用的是同一份排序，
    所以两类批注指到的是同一道题。

    JD 原文必须一起交给评审：`source=jd` 的题引用的是岗位要求，只给简历时评审会把
    合法引用判成"查无此句"（实测 2026-10-20，第一次端到端 15 条批注里 9 条是这类误报，
    而误报会一直跟着这批题）。它要看的是"这句话撑不撑得起题干"，前提是先看见出处。
    """
    return prompt.render(
        parsed_json=dump(parsed),
        jd_text=jd_text,
        questions_json=dump(
            [{field: question.get(field) for field in _REVIEW_FIELDS} for question in questions]
        ),
    )
