"""出题节点：扇出准备 + 逐条 `plan.items` 并行出题〔阶段 7 · §10.4.6〕

拆成两个可调用，因为 `Send` 扇出在图上是"一个节点派 N 个分支"的形状：

- `make_generate_node`：**扇出准备节点**（在 `PIPELINE` 主干里，被 `traced()` 包）。
  它不出题，只做四件事——检查 token 预算（M6 护栏，读注入的 `llm.usage`，不打模型）、
  自增批次号、用 `Overwrite` 清空上一批的题、声明本批用的模板版本。
  批次清空是计划书 §6 风险 5 的落点，机制实测见 `scripts/probe_send_fanout.py`。
- `make_generate_item_node`：**出题分支**。一条槽位一次 LLM 调用，产出该槽位的全部题目。
  它的入参是 `GenerateJob` 载荷而不是整个 state：`Send` 的载荷就是分支看到的全部输入，
  所以旧批次的 `questions` 天然看不到（这正是风险 6 要的"别让模型抄上一版"）。

节点契约（计划书 §4）在这里的落法：
- 只返回增量；分支返回 `{"questions": [本批新增]}`，由 `operator.add` 累加；
- `prompt_version` 只在扇出节点声明一次（分支与它同源），分支不写 trace；
- **可恢复失败**（某条槽位模型坏了、或题目被闸门全挡掉）写进 `errors` 并少出题，
  剩下的差额由 `review` 按"距 `plan.total` 少了几题"记成 `review_issues`（ADR-0015：
  批注只点名不回退，缺几题就少几题，报告照落）。
  分支自己不抛异常、也不计重试次数——一道槽位失败不该拖走其余五道；
- **不可恢复失败**两种（抛 `AppError` 子类）：没有配额表可扇出（`ValidationFailedError`）、
  token 预算已越过 `agent.max_tokens_per_run`（`BudgetExceededError`，M6 成本护栏）。
  护栏只在这里判一次也只需在这里判一次：扇出是全图最大的一笔开销，越线即断，
  后面一个字都不再花。

为什么不改成"一次调用出全部 15 题"：`default_max_tokens=2048` 装不下 15 题的题干 +
参考答案 + 评分标准，而一次长输出的任何一处 JSON 破损会让整批归零。扇出之后
单条槽位失败只影响那一条，重试也只重做失败那一档的预算。
"""

from typing import Any

from langgraph.types import Overwrite

from app.agents.state import AgentState
from app.agents.workflow import PROMPT_VERSION_KEY, FanoutNode, GenerateJob, Node
from app.config import Settings, get_settings
from app.exceptions.base import (
    BudgetExceededError,
    LLMError,
    LLMResponseError,
    ValidationFailedError,
)
from app.prompts.registry import Prompt, get_prompt
from app.services.question_service import (
    QuestionPayload,
    adopt_questions,
    dump,
    planned_count,
)
from app.utils.llm_client import LLMClient

#: 分支用的温度。判定类调用（match/validate/review）一律 0.0，出题要一点措辞多样性——
#: 扇出的各分支互相看不见在出什么题，抖一下能压住同主题各档撞出同一道题。
_TEMPERATURE = 0.3


def make_generate_node(
    *, llm: LLMClient | None = None, settings: Settings | None = None
) -> Node:
    """造扇出准备节点。它一次 LLM 都不花，只决定"这一轮扇给谁"。

    `llm` 参数不是用来调用的——成本护栏只读它的 `usage`（M6）。不传 `llm` 就没有护栏
    （图骨架测试与占位跑的默认），除了这一个注入点，这一节的三个动作（批次 +1、
    清空累积通道、声明模板版本）没有任何可调项——配额表由 `plan` 定死，
    行为差异不该在这里再开入口。
    """
    settings = settings or (llm.settings if llm is not None else get_settings())

    async def generate_node(state: AgentState) -> dict[str, Any]:
        plan = state.get("plan")
        items = [item for item in _dict_list(plan.get("items")) if _positive(item)] if plan else []
        if not items:
            raise ValidationFailedError(
                "没有出题配额，扇出没有依据",
                code="generate_no_plan",
                details={"resume_id": state.get("resume_id"), "plan": plan},
            )
        _require_budget(llm, settings)

        batch = int(state.get("question_batch") or 0) + 1
        prompt = get_prompt("generate")
        planned = planned_count(items)
        # `questions` 是累积通道：同一线程被重放时不整批换掉就会叠成 2N 题。
        # ADR-0015 之后一轮成卷，正常跑这里永远是批次 1。
        update: dict[str, Any] = {
            "question_batch": batch,
            "questions": Overwrite(value=[]),
            PROMPT_VERSION_KEY: f"{prompt.node}@{prompt.version}",
            "errors": [f"generate: 批次 {batch}，扇出 {len(items)} 条槽位，计划 {planned} 题"],
        }
        return update

    return generate_node


def make_generate_item_node(
    *, llm: LLMClient | None = None, settings: Settings | None = None
) -> FanoutNode:
    """造出题分支。一条 `plan.items` 槽位一次调用，产出该槽位的全部题目。"""
    settings = settings or (llm.settings if llm is not None else get_settings())
    client = llm or LLMClient(settings)
    prompt = get_prompt("generate")

    async def generate_item_node(job: GenerateJob) -> dict[str, Any]:
        item = job.get("plan_item") or {}
        batch = int(job.get("question_batch") or 0)
        raw_text = job.get("raw_text") or ""
        jd_text = job.get("jd_text") or ""
        count = int(item.get("count") or 0)
        topic = str(item.get("topic") or "")

        try:
            payload = await client.chat_json(
                "generate",
                _messages(prompt, job, count=count),
                schema=QuestionPayload,
                temperature=_TEMPERATURE,
            )
        except (LLMError, LLMResponseError) as exc:
            # 少出题不是故障终点：`review` 会把差额逐题记成批注（ADR-0015：只点名不回退）。
            # 这里抛异常会让整条图崩在一次调用的网络抖动上，代价太大
            return {"errors": [f"generate_item: 「{topic}」没出成（{exc.code}），本槽位 0 题"]}

        adopted, notes = adopt_questions(
            payload,
            item=item,
            batch=batch,
            order_start=int(job.get("order_start") or 0),
            raw_text=raw_text,
            jd_text=jd_text,
        )
        update: dict[str, Any] = {"questions": adopted}
        if notes:
            update["errors"] = [f"generate_item: {note}" for note in notes]
        return update

    return generate_item_node


def _require_budget(llm: LLMClient | None, settings: Settings) -> None:
    """成本护栏（M6）：本次运行的累计用量越线就不扇出。

    口径是 `llm.usage` 的 prompt+completion 合计——与 `eval_e2e` 报数的口径同源，
    包含 parse/validate/match/profile 花掉的与上一批出题花掉的。不传 `llm` 视为
    调用方不挂护栏（图骨架测试），不是"预算无限"。
    """
    if llm is None:
        return
    usage = llm.usage
    used = usage.prompt_tokens + usage.completion_tokens
    limit = settings.agent.max_tokens_per_run
    if used < limit:
        return
    raise BudgetExceededError(
        "token 用量已到单次运行上限，扇出前中断",
        details={"used_tokens": used, "limit_tokens": limit, "calls": usage.calls},
    )


def _messages(prompt: Prompt, job: GenerateJob, *, count: int) -> list[dict[str, str]]:
    """渲染出题 prompt。变量名与 `templates/v1/generate.user.j2` 一一对应。"""
    item = job.get("plan_item") or {}
    return prompt.render(
        parsed_json=dump(job.get("parsed") or {}),
        jd_text=job.get("jd_text") or "",
        roster_json=dump(job.get("roster") or []),
        difficulty=str(item.get("difficulty") or "medium"),
        source=str(item.get("source") or "generic"),
        topic=str(item.get("topic") or ""),
        count=count,
    )


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, dict)]


def _positive(item: dict[str, Any]) -> bool:
    return int(item.get("count") or 0) > 0
