"""AgentState TypedDict 与 reducer 定义〔阶段 7 · §10.3〕

通道约定（节点必须遵守，否则并行分支会互相覆盖）：
- 带 `Annotated[list[T], operator.add]` 的通道是**只追加**的：节点只返回增量，
  LangGraph 负责合并；返回整列表会把历史冲掉。
- 其余通道是最后写入者胜：节点返回 `{"字段": 新值}` 覆盖旧值。
- 累积通道要"整批换掉"时，写 `langgraph.types.Overwrite(value=新列表)` 绕过 reducer；
  同一个超步里只许出现一次，第二次直接抛 `InvalidUpdateError`（M5 的 `questions` 就靠它）。
- 节点接收 state 只读，绝不原地修改传入的 dict；返回值只写需要变更的键。
- 节点不许 raise 后吞掉：可恢复的失败写进 `errors`，不可恢复的抛 `AppError`。
"""

import operator
from enum import StrEnum
from typing import Annotated, Any, TypedDict


class NodeName(StrEnum):
    """节点的稳定名字，同时用作 trace 与边的标识。

    `GENERATE_ITEM` 不在 `PIPELINE` 里：它是 `generate` 按 `plan.items` 动态派发出来的
    工作单元，一次运行有几个取决于配额表，不是一条固定边。
    """

    PARSE = "parse"
    VALIDATE = "validate"
    MATCH = "match"
    PROFILE = "profile"
    PLAN = "plan"
    GENERATE = "generate"
    #: `generate` 的扇出工作节点（每个 `plan.items` 条目派一个）
    GENERATE_ITEM = "generate_item"
    REVIEW = "review"


#: 主干顺序；回退边由 workflow.py 的条件路由决定，不在此表达。
PIPELINE: tuple[NodeName, ...] = (
    NodeName.PARSE,
    NodeName.VALIDATE,
    NodeName.MATCH,
    NodeName.PROFILE,
    NodeName.PLAN,
    NodeName.GENERATE,
    NodeName.REVIEW,
)

#: 装配一张完整的图所必须有实现的名字 = 主干 + 扇出工作节点。
#: `build_agent_graph` 用它做缺件检查，测试的假节点表也用它凑齐——两处口径必须一致，
#: 否则"骨架测试能过"和"真图能装配"会变成两件事。
GRAPH_NODES: tuple[NodeName, ...] = (*PIPELINE, NodeName.GENERATE_ITEM)



class TraceStep(TypedDict, total=False):
    """一次节点执行的观测记录。"""

    node: str
    ok: bool
    ms: float
    detail: str
    #: 节点用到的 prompt 模板版本；计划 §4.4 要求它落进 trace，否则无法归因"哪版模板变差了"
    prompt_version: str



class AgentState(TypedDict, total=False):
    """一次"简历 -> 面试题"工作流的完整状态。

    `total=False`：节点每次只返回自己写的键，未写的键沿用上一个 checkpoint。
    """

    # --- 输入 ---
    resume_id: str
    jd_text: str
    #: 报告头 id。由调用方（API / 后台任务 / 脚本）在建好 `interview` 行后传入；
    #: `review` 收尾按它落库。缺省时收尾跳过落库并记进 `errors`——那是"调用方没打算入库"。
    interview_id: str

    # --- parse / validate ---
    raw_text: str
    parsed: dict[str, Any] | None
    validate_issues: list[str]
    validate_retries: int

    # --- match / profile / plan ---
    matched_gaps: list[dict[str, Any]]
    profile: dict[str, Any]
    plan: dict[str, Any]

    # --- generate / review ---
    #: **累积通道**（计划书 §6 风险 5）。`generate` 用 `Send` 按 `plan.items` 扇出后，
    #: 同一超步里有 N 个分支各写一批：覆盖语义会只剩一个分支的题，所以这里必须 `operator.add`。
    #: 代价是同一线程被重放时旧批次会跟着累加，因此扇出节点**自己**先写
    #: `{"questions": Overwrite(value=[])}` 清空历史，再让分支增量写；`Overwrite` 是
    #: langgraph 官方的"绕过 reducer 直接覆盖"哨兵，且一个超步只许出现一次。
    #: 每题都带 `batch` 字段，与 `question` 表的 `(interview_id, batch, order_index)` 唯一约束同源。
    questions: Annotated[list[dict[str, Any]], operator.add]
    #: 批次号，`generate` 每次扇出自增 1。收尾落库时写进 `question.batch`
    question_batch: int
    #: 纯观测：ADR-0015 起批注不再驱动路由，只标出"哪几题有问题"，题目一律照常落库
    review_issues: list[dict[str, Any]]

    # --- 收尾 ---
    finished: bool

    # --- 只追加通道 ---
    trace: Annotated[list[TraceStep], operator.add]
    errors: Annotated[list[str], operator.add]
