"""主工作流 StateGraph 组装：七节点 + 回退边 + 并行扇出〔阶段 7 · §10.5〕

本文件提供**组装设施**：checkpoint 存储、线程配置、两张有界路由、节点计时包装，
以及 `build_agent_graph()`。七个业务节点到位前，用它传入任意实现即可跑通全图。

M5 起 `generate` 是一段扇出：`plan.items` 有几条就派几个工作分支，所以
`NodeName.GENERATE` 到 `NodeName.REVIEW` 之间不再是固定边，而是
`generate --(Send)--> generate_item --> review`。分支的输入契约见 `GenerateJob`。

ADR-0015 删掉了 `review → generate` 那条回退边：`review` 只批注不拦门，跑完直接 END，
所以这里只剩 `validate → parse` 一张回退边。
"""

import uuid
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter
from typing import Any, TypedDict, cast

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.agents.state import GRAPH_NODES, PIPELINE, AgentState, NodeName, TraceStep
from app.config import PROJECT_ROOT, get_settings


class GenerateJob(TypedDict, total=False):
    """`Send` 给单个出题分支的载荷——**它就是分支看到的全部输入**。

    实测（langgraph 1.2.15，`scripts/probe_send_fanout.py`）：分支收到的是这份载荷本身，
    不是"主图 state + 载荷"，所以出题要用的上下文必须在这里点齐；反过来说，
    不该带的东西（上一批的 `questions`）不会漏进来，分支也就无从参照旧题。

    通道名的键（`question_batch` / `raw_text` / `parsed`）与主图同名，
    是因为分支返回值仍按主图 reducer 合并——同名只影响读，不影响写。
    """

    #: 一条配额槽位（`plan.items` 的元素）：`topic` / `difficulty` / `count` / `source` / `evidence`
    plan_item: dict[str, Any]
    #: 这道题在整份报告里的起始序号 = 前面各槽位 `count` 之和。
    #: 序号在扇出**之前**算好，落库的 `order_index` 才与分支完成顺序无关
    order_start: int
    question_batch: int
    raw_text: str
    #: `source=jd` 的槽位要引用 JD 原句，所以 JD 全文也得给分支——分支看不到主图 state，
    #: 少给这一个通道，那一类题就永远带不上可回溯引用（实测 2026-10-20 首次端到端跑 25%）
    jd_text: str
    #: 同批其它槽位的摘要（`topic` / `difficulty` / `count`，不含本槽位）。
    #: 扇出的固有盲区：分支看不见兄弟分支在出什么题，于是同主题的各难度档会各自写出
    #: "那道最经典的题"。实测（2026-10-20）第三轮批次里 5/15 题被评审判成换皮重复，
    #: 全在这一类上——给一张别的槽位问什么的摘要，比事后整批重做便宜得多
    roster: list[dict[str, Any]]
    parsed: dict[str, Any] | None


#: 节点签名：接收当前 state，返回要写入的通道增量（只写自己负责的键）。
Node = Callable[[AgentState], Coroutine[Any, Any, dict[str, Any]]]

#: 扇出分支的签名。它的入参是 `GenerateJob` 而不是 `AgentState`——`Send` 的载荷就是
#: 分支的全部输入（实测见 `scripts/probe_send_fanout.py`），所以这里必须与主干分开。
FanoutNode = Callable[[GenerateJob], Coroutine[Any, Any, dict[str, Any]]]

#: 扇出路由签名：返回一批 `Send`（并行派发），或一个节点名（本轮不扇出）。
Router = Callable[[AgentState], list[Send] | str]

#: 保留键：节点用它声明所用的 prompt 模板版本（§4 契约 4）。`traced()` 取走后写进
#: trace 步骤，并从通道更新里剔除——`AgentState` 没有这个通道，留着会污染 checkpoint。
PROMPT_VERSION_KEY = "prompt_version"

CHECKPOINT_DB = PROJECT_ROOT / "data" / "checkpoints.db"


@asynccontextmanager
async def open_checkpointer(
    conn_string: str | Path | None = None,
) -> AsyncIterator[AsyncSqliteSaver]:
    """打开 SQLite checkpoint 库。默认落在 `data/checkpoints.db`，与业务库分离。"""
    path = Path(conn_string) if conn_string is not None else CHECKPOINT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        yield saver


def make_thread_config(
    thread_id: str | None = None,
    *,
    recursion_limit: int | None = None,
) -> dict[str, Any]:
    """一次运行的 RunnableConfig。`thread_id` 决定 checkpoint 归属。"""
    agent = get_settings().agent
    return {
        "configurable": {"thread_id": thread_id or uuid.uuid4().hex},
        "recursion_limit": recursion_limit or agent.recursion_limit,
    }


def route_after_validate(state: AgentState) -> str:
    """信息校验的质量门：有问题且重做预算未用完则回到 parse 重新抽取（§10.4.2）。

    计数由 validate 节点在判定失败时自增，所以这里用 `<=`：`validate_max_retries=2`
    表示 parse 最多额外重做 2 次，共执行 3 次。用 `<` 会少跑一轮。
    """
    agent = get_settings().agent
    if (
        state.get("validate_issues")
        and state.get("validate_retries", 0) <= agent.validate_max_retries
    ):
        return NodeName.PARSE.value
    return NodeName.MATCH.value


def route_after_generate(state: AgentState) -> list[Send] | str:
    """`generate` 的扇出路由：一条 `plan.items` 槽位派一个出题分支（§10.4.6）。

    两个"不扇出"的出口都有真实调用方，不是防御性摆设：

    - `question_batch` 为 0：本轮根本没进扇出（图骨架测试里 `generate` 是占位节点）。
      批次号由 `generate` 节点自己 +1，路由只读它，所以这里不可能出现"扇了但没记批次"。
    - 槽位为空：`plan` 没给出任何配额（上游 `plan` 节点在有画像时必然给出至少一条），
      直落 `review` 让它按"这批题数量不足"判失败，比在这里抛异常多烧一次 checkpoint。

    `order_start` 用 `count` 的**计划值**累加而不是实际出题数：并行分支各自只知道
    自己那一段的全局偏移，才能把 `order_index` 落得与完成顺序无关。
    """
    batch = int(state.get("question_batch") or 0)
    items = _plan_items(state.get("plan"))
    if not batch or not items:
        return NodeName.REVIEW.value

    parsed = state.get("parsed") if isinstance(state.get("parsed"), dict) else None
    raw_text = state.get("raw_text") or ""
    jd_text = state.get("jd_text") or ""
    active = [item for item in items if int(item.get("count") or 0) > 0]
    entries = _roster(active)
    sends: list[Send] = []
    cursor = 0
    for position, item in enumerate(active):
        count = int(item.get("count") or 0)
        sends.append(
            Send(
                NodeName.GENERATE_ITEM.value,
                GenerateJob(
                    plan_item=item,
                    order_start=cursor,
                    question_batch=batch,
                    raw_text=raw_text,
                    jd_text=jd_text,
                    roster=[e for index, e in enumerate(entries) if index != position],
                    parsed=parsed,
                ),
            )
        )
        cursor += count
    return sends or NodeName.REVIEW.value


def _roster(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """配额表摘要：只给主题、档位、题数。分支要的是"别的槽位在问哪一档"，不是它们的原文引用。"""
    return [
        {
            "topic": str(item.get("topic") or ""),
            "difficulty": str(item.get("difficulty") or "medium"),
            "count": int(item.get("count") or 0),
        }
        for item in items
    ]


def _plan_items(plan: Any) -> list[dict[str, Any]]:
    if not isinstance(plan, Mapping):
        return []
    items = plan.get("items")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return []
    # 复制一份再塞进 `Send` 载荷：分支拿到的是 checkpoint 里 `plan` 的副本，改不动原表
    return [dict(item) for item in items if isinstance(item, Mapping)]


def traced(name: NodeName, node: Node) -> Node:
    """包一层计时，把每步执行写进只追加的 `trace` 通道。

    失败时只重抛、不写 state：LangGraph 的节点无法既返回通道更新又向上抛异常，
    所以异常路径由调用方（API/后台任务）负责记录，checkpoint 停在失败前一步。
    """

    async def wrapper(state: AgentState) -> dict[str, Any]:
        started = perf_counter()
        update = dict(await node(state))
        version = str(update.pop(PROMPT_VERSION_KEY, ""))
        return {"trace": [_trace_step(name, started, prompt_version=version)], **update}

    return wrapper


def _trace_step(
    name: NodeName,
    started: float,
    *,
    ok: bool = True,
    detail: str = "",
    prompt_version: str = "",
) -> TraceStep:
    step: TraceStep = {
        "node": name.value,
        "ok": ok,
        "ms": round((perf_counter() - started) * 1000, 2),
    }
    if detail:
        step["detail"] = detail
    if prompt_version:
        step["prompt_version"] = prompt_version
    return step


def build_agent_graph(
    nodes: Mapping[NodeName, Node | FanoutNode],
    *,
    checkpointer: AsyncSqliteSaver | None = None,
    wrap_nodes: bool = True,
) -> Any:
    """按 `PIPELINE` 主干 + `generate` 扇出 + 一张回退边装配，返回已 compile 的图。

    `nodes` 必须给全 `GRAPH_NODES`（主干七个节点**和**出题分支）：缺任何一个都在装配期报错，
    而不是等到运行中途。装配表混装两类签名（主干读 `AgentState`、出题分支读 `GenerateJob`），
    运行期谁读哪种由路由函数保证，所以这里用 `cast` 点明一次，而不是让节点去猜。

    出题分支不被 `traced()` 包：一次批次派 N 份，全记进 trace 会把"七个阶段"的观测面冲花；
    它用的模板与 `generate` 同源，版本号由 `generate` 那一步代记。
    """
    missing = [name.value for name in GRAPH_NODES if name not in nodes]
    if missing:
        raise ValueError(f"缺少节点实现：{missing}")

    graph = StateGraph(AgentState)
    for name in PIPELINE:
        impl = traced(name, cast("Node", nodes[name])) if wrap_nodes else nodes[name]
        # langgraph 1.2.15 + mypy 2.4.0：add_node 的重载连裸 `async def node(s: S)` 都不接受
        # （两种调用形式均在临时文件里复现），故定向忽略；运行时装配顺序由测试覆盖。
        graph.add_node(name.value, impl)  # type: ignore[call-overload]
    graph.add_node(
        NodeName.GENERATE_ITEM.value,
        cast("FanoutNode", nodes[NodeName.GENERATE_ITEM]),  # type: ignore[call-overload]
    )

    graph.add_edge(START, PIPELINE[0].value)
    graph.add_edge(NodeName.PARSE.value, NodeName.VALIDATE.value)
    graph.add_conditional_edges(
        NodeName.VALIDATE.value,
        route_after_validate,
        [NodeName.PARSE.value, NodeName.MATCH.value],
    )
    graph.add_edge(NodeName.MATCH.value, NodeName.PROFILE.value)
    graph.add_edge(NodeName.PROFILE.value, NodeName.PLAN.value)
    graph.add_edge(NodeName.PLAN.value, NodeName.GENERATE.value)
    graph.add_conditional_edges(
        NodeName.GENERATE.value,
        route_after_generate,
        [NodeName.GENERATE_ITEM.value, NodeName.REVIEW.value],
    )
    graph.add_edge(NodeName.GENERATE_ITEM.value, NodeName.REVIEW.value)
    # ADR-0015：review 只批注不拦门，没有"回 generate 重掷"这条路了
    graph.add_edge(NodeName.REVIEW.value, END)
    return graph.compile(checkpointer=checkpointer)
