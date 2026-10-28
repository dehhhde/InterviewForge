"""主工作流 StateGraph 组装：七节点 + 回退边 + 并行扇出〔阶段 7 · §10.5〕

本文件提供**组装设施**：checkpoint 存储、线程配置、三张路由（两张有界回退 + 一张扇出）、
节点计时包装，以及 `build_agent_graph()`。七个业务节点到位前，用它传入任意实现即可跑通全图。

M5 起 `generate` 是一段扇出：`plan.items` 有几条就派几个工作分支，所以
`NodeName.GENERATE` 到 `NodeName.REVIEW` 之间不再是固定边，而是
`generate --(Send)--> generate_item --> review`。分支的输入契约见 `GenerateJob`。

ADR-0015 删掉了"批注占比过阈值就回炉整批"那道门。ADR-0021 在**满足它留下的前提**之后
重新接了一条有界回路：调用方显式授权（`auto_fix=true`）、载荷里带上上一版的题目与点名它的
批注（`GenerateJob.previous`）、且最多回一轮。所以图上有两张条件边：
`validate → parse`（原样）与 `review → generate`（定向重做，`route_after_review`）。
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
from app.exceptions.base import AppError
from app.services.question_service import corpora_for
from app.services.redo_service import plan_slots, redo_plan


class GenerateJob(TypedDict, total=False):
    """`Send` 给单个出题分支的载荷——**它就是分支看到的全部输入**。

    实测（langgraph 1.2.15，`scripts/probe_send_fanout.py`）：分支收到的是这份载荷本身，
    不是"主图 state + 载荷"，所以出题要用的上下文必须在这里点齐；反过来说，
    不该带的东西（上一批的 `questions`）不会漏进来，分支也就无从参照旧题。

    通道名的键（`question_batch` / `raw_text` / `parsed`）与主图同名，
    是因为分支返回值仍按主图 reducer 合并——同名只影响读，不影响写。

    两份语料（`raw_text` / `jd_text`）**按槽位来源取值，但键恒在**〔T-205〕：
    `source=resume` 的分支不带 JD 原文，`source=jd` 的不带简历原文，`generic` 两份都带。
    判据来自 `question_service.corpora_for`——它同时是闸门"去哪儿核引用"的判据，
    所以少带一份绝不会让本该通过的题被丢弃。键不能省是因为分支读的是
    `job.get(...)`：缺键和"这一档没有语料"长得一模一样，而后者会静默少一整个来源的引用。
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
    #: 定向重做轮才非空〔T-404〕：这一档上一版的题目，每条带 `issues`（点名它的批注）。
    #: 它是 ADR-0015 那条前提的兑现——"批注进得了载荷"，回炉才不再是盲重掷。
    #: 只给**同槽位**的旧题：分支隔离是 ADR-0010 的结论，跨槽位可见只会让模型照着邻居改自己。
    previous: list[dict[str, Any]]


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

#: `traced()` 把失败的那一步挂到异常上的属性名。为什么不走 state 通道：节点抛异常时
#: 没有任何通道更新能落库（checkpoint 停在失败前一步），而"停在哪个节点、因为什么码"
#: 又只有包装层知道——所以由它交给抛出方，`app/tasks/question_generation.py` 收。
FAILED_STEP_ATTR = "if_trace_failed_step"

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


async def read_thread_state(
    thread_id: str,
    *,
    checkpoint_path: str | Path | None = None,
) -> dict[str, Any]:
    """按线程 id 读一次运行的图终态（只读）。线程不存在返回空 dict。

    为什么要**显式拿着 `thread_id`**〔T-304〕：以前"读某份报告的图终态"只能扫全部线程
    再逐个 `aget_state` 按 `interview_id` 认（`export_report_md.py` 的老路），
    而 checkpoint 库实测 21.77MB、每跑一次就加一批行。报告行现在自己记着线程 id，
    于是这是一次定位而不是一次遍历。

    为什么挂一个空节点的最小图，而不是自己 `saver.aget_tuple()` 再拆 checkpoint：
    channel 值的**反序列化是 `StateGraph` 做的**，复刻它那套字段就是照抄一个
    没有兼容性承诺的格式，langgraph 一升级就悄悄读到旧形状。图要求有入口点，
    所以那两条 `START → parse → END` 边省不掉。
    节点名必须点齐 `GRAPH_NODES`（含出题分支）：`generate` 的 pending sends 里存着
    `generate_item`，图里没这个名字时 langgraph 给每个分支打一行
    "Ignoring unknown node name"——我们只读值，但日志得干净。
    """
    if not thread_id:
        return {}

    async def _noop(_state: AgentState) -> dict[str, Any]:
        """壳图的占位节点：只读 channel 值，不跑任何东西。

        写成**具名且带标注**的函数不是风格：`lambda _state: {}` 会让 mypy 把 `NodeInputT`
        解成 `Never`，而那份"解不开"顺着同一个类对象串到本文件的 `build_agent_graph`
        （实测：带 lambda 的那版一进来，那边两条既有 `# type: ignore` 一起改报 `arg-type`）。
        `add_node` 自身仍撞 langgraph 1.2.15 + mypy 2.4.0 的重载毛病，照既有口径定向忽略。
        """
        return {}

    async with open_checkpointer(checkpoint_path) as saver:
        builder = StateGraph(AgentState)
        for name in GRAPH_NODES:
            builder.add_node(str(name), _noop)  # type: ignore[call-overload]
        builder.add_edge(START, str(NodeName.PARSE))
        builder.add_edge(str(NodeName.PARSE), END)
        graph = builder.compile(checkpointer=saver)
        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        return dict(snapshot.values or {})


def route_after_validate(state: AgentState) -> str:
    """信息校验的质量门：**规则问题**且重做预算未用完则回到 parse 重新抽取（§10.4.2）。

    只读 `validate_issues`，不读 `reflect_issues`〔T-201 / ADR-0022〕：反思挑的是
    "抽取层的设计缺口"，而 parse 是确定性的——重跑给出逐字相同的 `parsed`，修不掉任何东西。
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
    """照 `fanout_slots` 打包 `Send`——**这一轮扇给谁由 `generate` 节点定**〔T-404〕。

    路由自己不挑槽位：第一轮是整卷、定向重做轮只有被点名的档，两套选择共用这一个打包形状。
    `order_start` 也从通道里读，不在这里重新累加——重算一次就是第二个号位口径，
    而号位错了报告就会出现重号或空洞（ADR-0010）。

    两个"不扇出"的出口都有真实调用方，不是防御性摆设：

    - `question_batch` 为 0：本轮根本没进扇出（图骨架测试里 `generate` 是占位节点）。
      批次号由 `generate` 节点自己 +1，路由只读它，所以这里不可能出现"扇了但没记批次"。
    - `fanout_slots` 为空：`generate` 没给出计划（占位节点不写这个通道）。直落 `review`
      让它按"这批题数量不足"记批注，比在这里抛异常多烧一次 checkpoint。
    """
    batch = int(state.get("question_batch") or 0)
    slots = _fanout_slots(state.get("fanout_slots"))
    if not batch or not slots:
        return NodeName.REVIEW.value

    parsed = state.get("parsed") if isinstance(state.get("parsed"), dict) else None
    raw_text = state.get("raw_text") or ""
    jd_text = state.get("jd_text") or ""
    entries = _roster([dict(slot.get("plan_item") or {}) for slot in slots])
    sends: list[Send] = []
    for position, slot in enumerate(slots):
        item = dict(slot.get("plan_item") or {})
        # 语料按槽位来源带〔T-205〕：`verify_evidence` 只用 `corpora_for` 点名的那一份核对，
        # 另一份带过去既进不了闸门也进不了题目，只按分支数翻倍计费。
        # **键一个都不能少**：缺键与"忘了带"在分支里长得一模一样
        # （实测 2026-10-20 漏 `jd_text` 让 `source=jd` 的引用率掉到 25%），
        # 所以这里清的是值，不是字段。
        slot_raw, slot_jd = _slot_corpora(str(item.get("source") or ""), raw_text, jd_text)
        sends.append(
            Send(
                NodeName.GENERATE_ITEM.value,
                GenerateJob(
                    plan_item=item,
                    order_start=int(slot.get("order_start") or 0),
                    question_batch=batch,
                    raw_text=slot_raw,
                    jd_text=slot_jd,
                    roster=[e for index, e in enumerate(entries) if index != position],
                    parsed=parsed,
                    previous=[dict(q) for q in _slot_previous(slot.get("previous"))],
                ),
            )
        )
    return sends or NodeName.REVIEW.value


def route_after_review(state: AgentState) -> str:
    """`review` 之后的去留〔T-404 / ADR-0021〕：只有显式授权的第一轮才可能回炉。

    这不是把 ADR-0015 删掉的 `should_redo` 请回来，三条区别都在码上：

    - **要不要修由调用方决定**（`auto_fix`），这里没有任何"批注占比 / 阈值 / 值不值得"的判定；
      判据只回答"有没有题被点名"——存在性，不是比例。
    - **回炉带着反馈**：`fanout_slots` 里每个重做的槽位都带着上一版的题与点名它们的批注，
      所以第二次不是同一 prompt 的盲重掷（那正是 ADR-0015 下线旧门的理由）。
    - **最多一轮**：`question_batch == 1` 才路由得回去。成本上界是"多一轮扇出"，
      而不是受 `recursion_limit`（一个 30 的数字）约束。

    `auto_fix=false`（默认）时这张路由恒回 `END`——一次成卷是既有行为，回归它只要一个键。
    """
    if not state.get("auto_fix"):
        return END
    batch = int(state.get("question_batch") or 0)
    if batch != 1:
        return END
    plan = redo_plan(
        slots=plan_slots(state.get("plan")),
        questions=state.get("questions") or [],
        issues=state.get("review_issues") or [],
        reviewed_batch=batch,
        carry_batch=batch + 1,
    )
    return NodeName.GENERATE.value if plan.slots else END


def _fanout_slots(value: Any) -> list[Mapping[str, Any]]:
    """`fanout_slots` 通道的形状检查（checkpoint 回来的是普通 dict/list，不是 TypedDict）。"""
    if not isinstance(value, (Sequence,)) or isinstance(value, (str, bytes)):
        return []
    return [slot for slot in value if isinstance(slot, Mapping)]


def _slot_previous(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, (Sequence,)) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _slot_corpora(source: str, raw_text: str, jd_text: str) -> tuple[str, str]:
    """这个槽位要带哪几份语料。判据取自 `question_service.corpora_for`，不在这里另立一套。"""
    wanted = set(corpora_for(source))
    return (raw_text if "raw_text" in wanted else "", jd_text if "jd_text" in wanted else "")


def _roster(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """配额表摘要：主题、档位、题数、角度。分支要的是"别的槽位在问哪一档、打算从哪儿问"，
    不是它们的原文引用。

    `angles` 进来之后 `roster` 没有退掉，是实测后的决定（T-202）：分支自己看不见兄弟分支，
    角度只能靠这份摘要传过去；退掉它等于把"整批问了哪些角度"重新变成盲区。
    """
    return [
        {
            "topic": str(item.get("topic") or ""),
            "difficulty": str(item.get("difficulty") or "medium"),
            "count": int(item.get("count") or 0),
            "angles": [str(angle) for angle in (item.get("angles") or [])],
        }
        for item in items
    ]


def traced(name: NodeName, node: Node) -> Node:
    """包一层计时，把每步执行写进只追加的 `trace` 通道。

    失败时只重抛、不写 state：LangGraph 的节点无法既返回通道更新又向上抛异常，
    所以异常路径由调用方（API/后台任务）负责记录，checkpoint 停在失败前一步。
    "记录"的载体是挂在异常上的那一步 `ok=False` 的 `TraceStep`（`failed_trace_step()` 取），
    节点名与错误码在这里才算得出来——调用方只拿到异常，拿不到"当时在跑哪个节点"。
    """

    async def wrapper(state: AgentState) -> dict[str, Any]:
        started = perf_counter()
        try:
            update = dict(await node(state))
        except Exception as exc:
            step = _trace_step(name, started, ok=False, detail=_error_code(exc))
            setattr(exc, FAILED_STEP_ATTR, step)
            raise
        version = str(update.pop(PROMPT_VERSION_KEY, ""))
        return {"trace": [_trace_step(name, started, prompt_version=version)], **update}

    return wrapper


def _error_code(exc: BaseException) -> str:
    """失败步骤里的归因码：业务异常用自己的 `code`，其余按类型名（`KeyError` 这种没码可给）。"""
    return exc.code if isinstance(exc, AppError) else type(exc).__name__


def failed_trace_step(exc: BaseException) -> TraceStep | None:
    """读出 `traced()` 挂在异常上的失败步骤；不是节点抛的就返回 None。"""
    found = getattr(exc, FAILED_STEP_ATTR, None)
    return cast("TraceStep", found) if isinstance(found, dict) else None


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
    """按 `PIPELINE` 主干 + `generate` 扇出 + 两张有界回退装配，返回已 compile 的图。

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
    # ADR-0021（在 ADR-0015 的前提被兑现之后）：`auto_fix=true` 且第一轮评审点过名，
    # 才回 `generate` 重做那几档；其余一切情况仍然是 `review` 收尾直接 END。
    graph.add_conditional_edges(
        NodeName.REVIEW.value,
        route_after_review,
        [NodeName.GENERATE.value, END],
    )
    return graph.compile(checkpointer=checkpointer)
