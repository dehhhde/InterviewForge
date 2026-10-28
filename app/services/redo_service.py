"""定向重做的选槽判据〔S4 · T-404 / EXT-4〕

纯函数：不碰 LLM、不碰数据库、零成本、可单测。分工到这一层为止——
**要不要修由调用方显式授权**（`auto_fix=true`），这里只回答"修谁、带什么进载荷"。

判据只有一条：**被点名（批注的 `index` 指得到题）的题所属的那一档整档重做**。
ADR-0015 删掉的那套东西不在这里复活：没有"占比"、没有"阈值"、没有"值不值得回炉"的判定，
所以也不会冒出第二套标准。

四条边界（每条都有对应的用例钉住）：

- `index=None` 的批注**点不到具体槽位**，不进重做。缺题类批注说的是"整批少了几题"，
  把它的 `fix` 抄给某个分支并不会补上那几题——分支只知道自己的 `count`。
- 重做的是**整档**而不是单题：槽位的 `count` 与 `order_start` 原样保留，新题占回原来的号位，
  所以一轮修复不会改乱配额表，也不会让报告里出现空洞或重号。
- 没被点名的槽位一题不动，由扇出节点**原样带进新批次**（`carried`）。否则"最新批次"
  就只剩被修过的那几题，`list_for_interview` 按最新批读出来的卷子会缺页。
- 一个旧题的批注只跟着**它自己**进载荷（`previous`），不给别的槽位看——
  分支隔离是 ADR-0010 的既有结论，跨槽位可见会造出"照着邻居的批注改自己的题"。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypedDict

from app.services.question_service import order_questions

#: 进载荷的旧题只带"看得懂自己要被换掉"所需的字段。`reference_answer` / `rubric` 不给：
#: 重做的是题干与考察点，把上一版的完整答案再喂一遍只会让模型抄回去（换皮重复的成因之一）。
_PREVIOUS_FIELDS: tuple[str, ...] = ("stem", "difficulty", "source", "evidence")


class FanoutSlot(TypedDict, total=False):
    """一轮扇出里的一个槽位。`generate` 节点算好它，`route_after_generate` 只负责打包。

    `previous` 只在定向重做那一轮出现（第一轮恒缺省）：它是这一档上一版的题目，
    每条自带 `issues`（点名它的那几条批注）。分支拿到的就是这些，看不见别的槽位。
    """

    plan_item: dict[str, Any]
    order_start: int
    previous: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class RedoPlan:
    """一次定向重做的两段结果——**必须一起看**，分开用就会造出缺页的卷子。

    `slots` 是要重出的槽位（含载荷要的 `previous`）；`carried` 是不动的那些题，
    批次号已经换成新批次，直接进累积通道的 `Overwrite` 就是。
    """

    slots: list[FanoutSlot]
    carried: list[dict[str, Any]]


def plan_slots(plan: Any) -> list[FanoutSlot]:
    """整卷的扇出计划：`plan.items` 里每个 `count > 0` 的槽位一项，号位按计划题数累加。

    这是**号位口径唯一的出处**：第一轮由它扇出，定向重做轮也从它出发切出被点名的档——
    两轮算两次就会有两个 `order_start`，而 `order_index` 撞车的表现是报告里少一题多一题。

    累加用**计划值**而不是实收题数（ADR-0010）：并行分支各自只知道自己的偏移，
    号位才与完成顺序无关。`count <= 0` 的槽位不进计划，和旧版路由的过滤口径一致。
    """
    items = plan.get("items") if isinstance(plan, Mapping) else None
    slots: list[FanoutSlot] = []
    cursor = 0
    for raw in items or ():
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        count = int(item.get("count") or 0)
        if count <= 0:
            continue
        slots.append(FanoutSlot(plan_item=item, order_start=cursor))
        cursor += count
    return slots


def redo_plan(
    *,
    slots: Sequence[FanoutSlot],
    questions: Sequence[Mapping[str, Any]],
    issues: Sequence[Mapping[str, Any]],
    reviewed_batch: int,
    carry_batch: int,
) -> RedoPlan:
    """按批注点名，把整卷扇出计划切成"重做这些档 / 那些原样带走"。

    `slots` 是整卷的扇出计划（`plan_slots` 的产物），`questions` 与 `issues` 是刚评审完
    的那一批题与批注。号位归槽位用的是 `order_index ∈ [order_start, order_start + count)`；
    而批注的 `index` 是**位置下标**，所以先经 `order_questions` 把下标换成题——这道换算必须
    与 `review` 交给评审时用的是同一份排序，否则批注会指错题（AGENTS.md §5）。
    """
    ordered = order_questions(questions)
    flagged = _flagged_texts(ordered, issues)
    claimed: set[int] = set()
    redo_slots: list[FanoutSlot] = []

    for slot in slots:
        item = slot.get("plan_item") or {}
        start = int(slot.get("order_start") or 0)
        stop = start + int(item.get("count") or 0)
        members = [
            position
            for position, question in enumerate(ordered)
            if int(question.get("batch") or 0) == reviewed_batch
            and start <= int(question.get("order_index") or 0) < stop
        ]
        if not any(position in flagged for position in members):
            continue
        claimed.update(members)
        redo_slots.append(
            FanoutSlot(
                plan_item=dict(item),
                order_start=start,
                previous=[
                    _previous_view(ordered[position], flagged.get(position, ()))
                    for position in members
                ],
            )
        )

    carried = [
        {**question, "batch": carry_batch}
        for position, question in enumerate(ordered)
        if position not in claimed
    ]
    return RedoPlan(slots=redo_slots, carried=carried)


def _flagged_texts(
    ordered: Sequence[Mapping[str, Any]], issues: Sequence[Mapping[str, Any]]
) -> dict[int, list[str]]:
    """批注落到具体的题上：`{ordered 里的位置: [批注文本]}`。

    越界序号与 `index=None` 都在这里挡掉。挡掉而不是报错：序号是模型给的，
    `adopt_model_issues` 已经校验过一次，这里是载荷构造自己的那道——规则层给的一定在界内，
    但构造载荷不该依赖"上游一定没漏"。
    """
    hits: dict[int, list[str]] = {}
    for issue in issues:
        index = issue.get("index")
        if not isinstance(index, int) or not 0 <= index < len(ordered):
            continue
        text = _issue_text(issue)
        if text:
            hits.setdefault(index, []).append(text)
    return hits


def _previous_view(question: Mapping[str, Any], texts: Sequence[str]) -> dict[str, Any]:
    """一道旧题 + 点名它的那几条批注，这就是分支能看见的全部上一版。"""
    view: dict[str, Any] = {field: question.get(field) for field in _PREVIOUS_FIELDS}
    view["issues"] = list(texts)
    return view


def _issue_text(issue: Mapping[str, Any]) -> str:
    """批注正文配上它的修法。只看"哪儿不对"模型猜不出怎么改，只看"怎么改"会丢掉成因。"""
    problem = str(issue.get("problem") or "").strip()
    fix = str(issue.get("fix") or "").strip()
    if not problem:
        return ""
    return f"{problem}｜修法：{fix}" if fix else problem
