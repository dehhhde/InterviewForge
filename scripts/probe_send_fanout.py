"""M5 第一天探针：验证计划书 §6 风险 5 的机制（一次性脚本，不进测试；永久凭证是
`tests/test_agents/test_generate_node.py` 与 `test_m5_pipeline.py`）。

结论（实测 langgraph 1.2.15）：
1. `Send` 的载荷**完全替换** state——worker 只看得到载荷里的键，主图的 `raw_text`
   / `question_batch` 不会自动跟过去，必须显式打包；
2. 同一超步内 N 个分支的写入被**逐个**喂给 reducer（`BinaryOperatorAggregate.update`
   里 for 循环调 `operator(self.value, value)`），所以 `operator.add` 能收齐 15 题；
3. `Overwrite(value=[])` 绕过 reducer 直接覆盖，正好用来在扇出前清空旧批次；
   一个超步只许一次，第二次抛 `InvalidUpdateError`；
4. `Send` 目标节点下游的普通边在**所有分支完成后**只跑一次（汇聚点成立）；
5. 分支写入顺序按任务顺序，不按完成顺序——乱序 sleep 不影响通道最终顺序；
6. `traced()` 包装与扇出路由共存：路由函数不经过包装层，`prompt_version` 只在
   扇出节点那一步写进 trace（分支若也写会互相覆盖）。
"""

import asyncio
import json
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Overwrite, Send

from app.agents.state import AgentState, NodeName
from app.agents.workflow import PROMPT_VERSION_KEY, traced


class WorkerPayload(TypedDict, total=False):
    """`Send` 的载荷形状：worker 看到的**全部**输入。"""

    plan_item: dict[str, Any]
    question_batch: int
    raw_text: str


async def fanout_node(state: AgentState) -> dict[str, Any]:
    batch = int(state.get("question_batch", 0)) + 1
    return {
        "question_batch": batch,
        # 先清空，再让 N 个分支往空通道上累加
        "questions": Overwrite(value=[]),
        PROMPT_VERSION_KEY: "generate@v1",
        "errors": [f"generate: batch={batch} 扇出 {len(state['plan']['items'])} 条"],
    }


def route_fanout(state: AgentState) -> list[Send]:
    batch = int(state.get("question_batch", 0))
    return [
        Send(
            "generate_item",
            WorkerPayload(
                plan_item=item,
                question_batch=batch,
                raw_text=state.get("raw_text", ""),
            ),
        )
        for item in state["plan"]["items"]
    ]


async def item_node(state: WorkerPayload) -> dict[str, Any]:
    item = state["plan_item"]
    order = int(item["topic"][1:])
    # topic0 故意慢 50ms：验证合并顺序不随完成顺序漂
    await asyncio.sleep(0.05 if order == 0 else 0.001)
    return {
        "questions": [
            {"stem": f"q{order}", "batch": state.get("question_batch", 0), "order": order}
        ],
        "errors": [f"generate_item: 看见的键 = {sorted(state)}"],
    }


async def collect_node(state: AgentState) -> dict[str, Any]:
    # 汇聚点：只报数，不写 `plan`——那是最后写入者胜的通道，写就把扇出依据清掉了
    orders = [q["order"] for q in state.get("questions") or []]
    return {"errors": [f"collect: {len(orders)} 题，顺序 {orders}"]}


async def review_node(state: AgentState) -> dict[str, Any]:
    """假 review：只写批注并收尾（ADR-0015 之后评审是终点，不再有回退边）。"""
    return {
        "review_issues": [{"index": 0, "problem": "题干信息不全"}],
        "finished": True,
    }


async def run() -> AgentState:
    graph = StateGraph(AgentState)
    graph.add_node(NodeName.GENERATE.value, traced(NodeName.GENERATE, fanout_node))  # type: ignore[call-overload]
    graph.add_node("generate_item", item_node)  # type: ignore[call-overload]
    graph.add_node("collect", collect_node)  # type: ignore[call-overload]
    graph.add_node(NodeName.REVIEW.value, review_node)  # type: ignore[call-overload]
    graph.add_edge(START, NodeName.GENERATE.value)
    graph.add_conditional_edges(NodeName.GENERATE.value, route_fanout, ["generate_item"])
    graph.add_edge("generate_item", "collect")
    graph.add_edge("collect", NodeName.REVIEW.value)
    graph.add_edge(NodeName.REVIEW.value, END)
    return await graph.compile().ainvoke(
        {
            "plan": {"items": [{"topic": f"t{i}"} for i in range(15)]},
            "raw_text": "RESUME",
            # 预置一题"上一版残留"，验证 Overwrite 真把它清掉
            "questions": [{"stem": "stale", "batch": 0, "order": 99}],
        },
        {"recursion_limit": 40},
    )


result = asyncio.run(run())
print(
    json.dumps(
        {
            "final_batch": result["question_batch"],
            "question_count": len(result["questions"]),
            "batches_present": sorted({q["batch"] for q in result["questions"]}),
            "stale_survived": any(q["order"] == 99 for q in result["questions"]),
            "arrival_order": [q["order"] for q in result["questions"]],
            "trace": [(step["node"], step.get("prompt_version", "")) for step in result["trace"]],
            "finished": result.get("finished"),
            "worker_saw": next(e for e in result["errors"] if e.startswith("generate_item")),
            "collect_lines": [e for e in result["errors"] if e.startswith("collect")],
            "prompt_version_leaked": PROMPT_VERSION_KEY in result,
        },
        ensure_ascii=False,
    )
)
