"""定向重做的选槽判据〔S4 · T-404〕——纯函数，零成本，边界逐条钉住

这里断的全是**结构与批次**：谁被挑出来、号位对不对、没被点名的题有没有原样带走。
批注正文由测试自己造，所以可以断它进了载荷；模型文案一律不碰（AGENTS.md §6）。
"""

from typing import Any

import pytest

from app.services.redo_service import plan_slots, redo_plan

TOPICS = ("并发", "存储", "系统设计")
EVIDENCE = "使用 Redis 实现滑动窗口限流"
#: 三条槽位、每条 2 题：号位区间是 0-1 / 2-3 / 4-5
PLAN: dict[str, Any] = {
    "items": [
        {"topic": topic, "difficulty": "medium", "count": 2, "source": "resume"}
        for topic in TOPICS
    ],
    "total": 6,
}


def _question(topic: str, *, order_index: int, batch: int = 1) -> dict[str, Any]:
    """一道题的最小形状——只带判据读得到的那几个字段。"""
    return {
        "stem": f"关于{topic}的第 {order_index} 道题",
        "difficulty": "medium",
        "source": "resume",
        "evidence": EVIDENCE,
        "reference_answer": "讲清方案与边界",
        "rubric": ["能说出两种做法的取舍"],
        "topic": topic,
        "batch": batch,
        "order_index": order_index,
    }


def _batch_one() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for slot, topic in enumerate(TOPICS):
        for offset in range(2):
            rows.append(_question(topic, order_index=slot * 2 + offset))
    return rows


def _issue(
    index: int | None, *, problem: str = "题干信息不全", fix: str = "补上数据量级"
) -> dict[str, Any]:
    return {"index": index, "problem": problem, "fix": fix, "origin": "model"}


def _redo(issues: list[dict[str, Any]], *, questions: list[dict[str, Any]] | None = None):
    return redo_plan(
        slots=plan_slots(PLAN),
        questions=questions if questions is not None else _batch_one(),
        issues=issues,
        reviewed_batch=1,
        carry_batch=2,
    )


# --- plan_slots：号位口径的唯一出处 ---


def test_slot_offsets_come_from_planned_counts_not_from_questions() -> None:
    """`order_start` 按**计划**题数累加：分支并行完成，实收题数要汇合后才知道。"""
    slots = plan_slots(PLAN)

    assert [(slot["plan_item"]["topic"], slot["order_start"]) for slot in slots] == [
        ("并发", 0),
        ("存储", 2),
        ("系统设计", 4),
    ]


def test_slots_without_quota_are_skipped_and_do_not_shift_the_cursor() -> None:
    """`count<=0` 的槽位不进计划：扇出去只是一次白花的调用，而 `total` 本来也不含它。"""
    plan = {
        "items": [
            {"topic": "并发", "difficulty": "medium", "count": 0},
            {"topic": "存储", "difficulty": "hard", "count": 3},
        ],
        "total": 3,
    }

    assert [(slot["plan_item"]["topic"], slot["order_start"]) for slot in plan_slots(plan)] == [
        ("存储", 0)
    ]


@pytest.mark.parametrize("plan", [None, {}, {"items": None}, {"items": "并发"}, [1, 2]])
def test_a_missing_or_malformed_plan_yields_no_slots(plan: Any) -> None:
    assert plan_slots(plan) == []


# --- redo_plan：谁被点名、谁被带走 ---


def test_only_the_slot_that_was_named_is_redone() -> None:
    plan = _redo([_issue(2)])  # 2 号位属于「存储」

    assert [slot["plan_item"]["topic"] for slot in plan.slots] == ["存储"]
    assert plan.slots[0]["order_start"] == 2, "重做轮必须占回原号位，否则报告空洞或重号"


def test_an_issue_without_an_index_redoes_nothing() -> None:
    """`index=None` 说的是"整批少了几题"，抄给任何一条分支都补不上那几题。

    这条是"宁可不修也不盲修"的边界：缺题类批注进不了载荷，所以它一次扇出都不该换。
    """
    plan = _redo([_issue(None, problem="计划 6 题，实收 5 题", fix="")])

    assert plan.slots == []
    assert len(plan.carried) == 6, "没挑中任何一档时整批原样带走"


def test_an_out_of_range_index_is_dropped_not_wrapped() -> None:
    """越界序号当没给：负下标会**默默指到批尾**，而批尾正是该留着不动的那几题。"""
    plan = _redo([_issue(99), _issue(-1)])

    assert plan.slots == []
    assert len(plan.carried) == 6


def test_the_whole_slot_is_replaced_when_one_question_is_named() -> None:
    """点一档里的一题 = 整档重出：槽位的 `count` 与 `order_start` 原样保留。

    只重出那一题会让分支拿到 `count=1`，等于悄悄改写配额表，整份卷子的难度配比跟着变。
    """
    plan = _redo([_issue(3)])

    previous = plan.slots[0]["previous"]
    assert [item["stem"] for item in previous] == ["关于存储的第 2 道题", "关于存储的第 3 道题"]
    assert [item["issues"] for item in previous] == [[], ["题干信息不全｜修法：补上数据量级"]]


def test_a_previous_view_carries_no_answer_or_rubric() -> None:
    """旧题只带"知道自己要被换掉"所需的字段：把上一版答案再喂一遍就是照着抄。"""
    plan = _redo([_issue(0)])

    assert set(plan.slots[0]["previous"][0]) == {
        "stem",
        "difficulty",
        "source",
        "evidence",
        "issues",
    }


def test_annotations_stay_with_their_own_slot() -> None:
    """两条档都被点名时，各档只看得见点名自己的批注——分支隔离是 ADR-0010 的结论。"""
    plan = _redo(
        [
            _issue(0, problem="并发这一档太浅", fix=""),
            _issue(4, problem="系统设计在复述题干", fix=""),
        ]
    )

    by_topic = {slot["plan_item"]["topic"]: slot["previous"] for slot in plan.slots}
    assert set(by_topic) == {"并发", "系统设计"}
    assert [item["issues"] for item in by_topic["并发"]] == [["并发这一档太浅"], []]
    assert [item["issues"] for item in by_topic["系统设计"]] == [["系统设计在复述题干"], []]


def test_unnamed_questions_are_carried_with_their_number_and_a_new_batch() -> None:
    """带走的那几题换批次、不换号位：报告读的是最新批次，缺页比缺修更糟。"""
    plan = _redo([_issue(2)])

    assert {item["batch"] for item in plan.carried} == {2}
    assert [item["order_index"] for item in plan.carried] == [0, 1, 4, 5]


def test_a_question_from_another_batch_never_claims_a_slot() -> None:
    """号位区间只在**被评审的那一批**里认题（计划书 §6 风险 5：线程里可并存两批）。

    不认批次的话，旧批次的题会把槽位"抢"进重做，而批注其实指着新批次那道题。
    """
    mixed = [_question("并发", order_index=0, batch=1), _question("存储", order_index=2, batch=2)]
    plan = redo_plan(
        slots=plan_slots(PLAN),
        questions=mixed,
        issues=[_issue(0, problem="批次 2 的第一题太浅")],
        reviewed_batch=2,
        carry_batch=3,
    )

    assert plan.slots == [], "0 号位那道题属于批次 1，批次 2 的评审点不到它"
    assert [item["batch"] for item in plan.carried] == [3, 3]


def test_index_is_read_off_the_sorted_order_not_off_the_list_given() -> None:
    """批注下标的口径 = `order_questions` 的结果：分支到达顺序打乱也不能让它指错题。"""
    shuffled = [_question("系统设计", order_index=4), _question("并发", order_index=0)]
    plan = _redo([_issue(0, problem="第一题太浅", fix="")], questions=shuffled)

    assert plan.slots[0]["plan_item"]["topic"] == "并发"
    assert plan.slots[0]["previous"][0]["issues"] == ["第一题太浅"]


def test_a_fix_travels_with_the_problem_it_answers() -> None:
    """只给"哪儿不对"模型猜不出怎么改，只给"怎么改"会丢掉成因——载荷里两条都要在。"""
    plan = _redo([_issue(0, problem="第一题太浅", fix="改成问失效场景")])

    assert plan.slots[0]["previous"][0]["issues"] == ["第一题太浅｜修法：改成问失效场景"]


def test_an_issue_with_an_empty_problem_redoes_nothing() -> None:
    """空正文的批注不构造 `issues`：载荷里出现 `[""]` 时模型只会把旧题再抄一遍。"""
    assert _redo([_issue(0, problem="")]).slots == []
