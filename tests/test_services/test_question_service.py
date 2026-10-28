"""`question_service` 的闸门层测试〔M5〕

只断言结构与判据，不断言模型文案（AGENTS.md §6）：这里的输入是**手写的草稿**，
本来就不是模型产出，正好用来钉住"谁覆盖谁"——难度被配额表覆盖、引用被原文覆盖。
"""

from typing import Any

from app.services.question_service import (
    MAX_QUESTIONS_PER_ITEM,
    QuestionPayload,
    adopt_questions,
    corpora_for,
    evidence_problem,
    evidence_ratio,
    order_questions,
    planned_count,
    sort_key,
    to_row,
    verify_evidence,
)

#: 所有 `evidence` 的出处。真简历里这段话是一个整行，抽取层不会切碎它
RAW = "项目经历\n使用 Redis 实现滑动窗口限流，QPS 从 800 提升到 2000。\n负责订单域重构。"

#: `source=jd` 的出处。它和简历一样是"逐字核对"的对象，不是背景描述
JD = "岗位职责：负责高并发服务的容量规划与限流方案评审。"

#: JD 里可以被逐字引用的一段
JD_SENTENCE = "负责高并发服务的容量规划与限流方案评审"

EVIDENCE = "使用 Redis 实现滑动窗口限流"

_ITEM: dict[str, Any] = {"topic": "并发", "difficulty": "hard", "count": 2, "source": "resume"}


def _draft(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "stem": "讲讲你在订单服务里做过的限流方案",
        "difficulty": "hard",
        "source": "resume",
        "evidence": EVIDENCE,
        "checkpoints": ["是否理解限流算法的取舍"],
        "reference_answer": "讲清算法、边界与踩过的坑",
        "rubric": ["说清为什么选滑动窗口而不是令牌桶"],
        "follow_ups": ["热点 key 怎么处理"],
    }
    base.update(overrides)
    return base


def _payload(*drafts: dict[str, Any]) -> QuestionPayload:
    return QuestionPayload.model_validate({"questions": list(drafts)})


def _adopt(
    *drafts: dict[str, Any], item: dict[str, Any] = _ITEM, order_start: int = 0, jd_text: str = JD
) -> Any:
    return adopt_questions(
        _payload(*drafts),
        item=item,
        batch=1,
        order_start=order_start,
        raw_text=RAW,
        jd_text=jd_text,
    )


def test_difficulty_is_stamped_from_the_plan_item_not_the_model() -> None:
    """模型把 hard 槽位写成 easy 也得改回来：配额表要满足难度配比，措辞不归它管。"""
    adopted, notes = _adopt(_draft(difficulty="easy"), _draft())

    assert [q["difficulty"] for q in adopted] == ["hard", "hard"]
    assert any("改回 hard" in note for note in notes)


def test_a_resume_claim_without_verifiable_evidence_is_dropped() -> None:
    """无证据即丢弃：核不上出处的整题删掉，不降级成 generic 保留题干。"""
    adopted, notes = _adopt(
        _draft(evidence="我用 Kafka 做过异地多活"),
        _draft(evidence=""),
        _draft(),
    )

    assert len(adopted) == 1
    assert len(notes) >= 2
    assert any("找不到" in note for note in notes)
    assert any("没给原文引用" in note for note in notes)


def test_a_jd_sourced_question_quotes_the_job_ad() -> None:
    """`source=jd` 的出处是 JD 全文，不是简历：真简历跑出来的槽位 21/26 都是 jd 来源。"""
    adopted, notes = _adopt(
        _draft(source="jd", evidence="负责高并发服务的容量规划与限流方案评审"),
        item={**_ITEM, "source": "jd", "count": 1},
    )

    assert len(adopted) == 1
    assert adopted[0]["evidence"] == "负责高并发服务的容量规划与限流方案评审"
    assert notes == []


def test_a_jd_sourced_question_copying_the_resume_is_dropped() -> None:
    """来源与出处必须对得上：抄简历句子的 jd 题会在报告里谎称自己来自岗位要求。"""
    adopted, notes = _adopt(
        _draft(source="jd", evidence=EVIDENCE), item={**_ITEM, "source": "jd", "count": 1}
    )

    assert adopted == []
    assert any("岗位 JD 原文里找不到" in note for note in notes)


def test_a_jd_claim_is_dropped_when_there_is_no_job_ad_to_quote() -> None:
    """没有 JD 原文就无从核对——无据可查的"基于 JD 出题"和编造没法区分。"""
    adopted, notes = _adopt(
        _draft(source="jd", evidence="负责容量规划"), item={**_ITEM, "source": "jd", "count": 1},
        jd_text=""
    )

    assert adopted == []
    assert any("岗位 JD 原文里找不到" in note for note in notes)


def test_a_generic_question_may_quote_either_corpus() -> None:
    """generic 不声称出处，但带了引用就核（两处任一命中即可），核不上只置空。"""
    adopted, notes = _adopt(
        _draft(source="generic", evidence="负责高并发服务的容量规划"),
        _draft(source="generic", stem="第二题的题干要够长才能过 schema", evidence="编造的引用内容"),
        item={**_ITEM, "count": 2},
    )

    assert [q["evidence"] for q in adopted] == ["负责高并发服务的容量规划", ""]
    assert any("已置空" in note for note in notes)


def test_evidence_matching_ignores_whitespace_and_zero_width() -> None:
    """比对口径沿用抽取层：模型多塞一个空格不该让一道好题被判成幻觉。"""
    adopted, _ = _adopt(_draft(evidence=" 使用 Redis 实现滑动窗口\u200b限流 "))

    assert len(adopted) == 1
    assert adopted[0]["evidence"] == "使用 Redis 实现滑动窗口限流"


def test_unverifiable_evidence_on_a_non_resume_question_is_blanked_not_fatal() -> None:
    """题干不声称基于简历时，脏引用置空即可——丢了它题还能用。"""
    adopted, notes = _adopt(
        _draft(source="generic", evidence="我用 Kafka 做过异地多活"), item={**_ITEM, "count": 1}
    )

    assert adopted[0]["source"] == "generic"
    assert adopted[0]["evidence"] == ""
    assert any("已置空" in note for note in notes)


def test_an_unknown_source_is_treated_as_generic() -> None:
    """表外的来源取值一律按最保守的 `generic`：不替模型声称"基于简历"。"""
    adopted, _ = _adopt(_draft(source="resume_v2"), item={**_ITEM, "count": 1})

    assert adopted[0]["source"] == "generic"


def test_the_gate_reads_exactly_the_corpora_the_fanout_packs() -> None:
    """扇出瘦身的正确性前提：少带一份原文，绝不改变任何一题的通过与否〔T-205〕。

    `route_after_generate` 用 `corpora_for` 决定每个槽位带哪份语料，`verify_evidence` 用
    同一个判据决定拿哪份去核。所以"这一档没带的那份"在闸门眼里本来就不存在——
    这里把它显式传成空串，判定必须和两份都给时一字不差。
    """
    assert corpora_for("resume") == ("raw_text",)
    assert corpora_for("jd") == ("jd_text",)
    #: `generic` 与任何表外来源：不声称出处，两份都核
    assert corpora_for("generic") == corpora_for("resume_v2") == ("raw_text", "jd_text")

    assert verify_evidence("resume", EVIDENCE, raw_text=RAW, jd_text="") == (EVIDENCE, None)
    assert verify_evidence("jd", JD_SENTENCE, raw_text="", jd_text=JD) == (JD_SENTENCE, None)
    assert verify_evidence("generic", EVIDENCE, raw_text=RAW, jd_text="") == (EVIDENCE, None)
    assert verify_evidence("generic", JD_SENTENCE, raw_text="", jd_text=JD) == (JD_SENTENCE, None)


def test_the_gate_ignores_the_corpus_the_source_did_not_claim() -> None:
    """上一条的对偶：两份语料都在场时，`resume` 档也只核简历。

    这是"一份判据两处用"的另一半——载荷按 `corpora_for` 清值之后，闸门的行为和清值之前
    完全一致；反过来，载荷带全也不会让"声称基于简历"的题拿 JD 句子当出处。
    """
    assert evidence_problem("resume", JD_SENTENCE, raw_text=RAW, jd_text=JD) is not None
    assert evidence_problem("jd", EVIDENCE, raw_text=RAW, jd_text=JD) is not None
    assert evidence_problem("resume", EVIDENCE, raw_text=RAW, jd_text=JD) is None
    assert evidence_problem("generic", EVIDENCE, raw_text=RAW, jd_text="") is None


def test_order_index_skips_no_holes_when_a_draft_is_dropped() -> None:
    """工序号按采纳成功发：被丢弃的题不能在报告里留下一个没有题的号。"""
    adopted, notes = _adopt(
        _draft(evidence="编造的引用", stem="这道题的题干本来也够长"),
        _draft(stem="第二题的题干要够长才能过 schema"),
        _draft(stem="第三题的题干也要够长"),
        item={**_ITEM, "count": 3},
        order_start=4,
    )

    assert [q["order_index"] for q in adopted] == [4, 5]
    assert [q["batch"] for q in adopted] == [1, 1]
    assert any("计划 3 题，实收 2 题" in note for note in notes)


def test_order_index_is_offset_per_slot_so_branches_do_not_collide() -> None:
    """`order_start` 由扇出前累加的计划数给出：两条槽位不会拿到同一个题号。"""
    first, _ = _adopt(_draft(), _draft(), order_start=0)
    second, _ = _adopt(_draft(), _draft(), order_start=2)

    assert [q["order_index"] for q in first] == [0, 1]
    assert [q["order_index"] for q in second] == [2, 3]


def test_a_short_stem_fails_the_schema_and_is_recorded_as_a_drop() -> None:
    adopted, notes = _adopt(_draft(stem="太短"), item={**_ITEM, "count": 1})

    assert adopted == []
    assert any("结构不合格" in note for note in notes)


def test_blank_checkpoints_never_reach_the_report() -> None:
    """空白考察点过不了 `QuestionSchema`，也就过不了这道闸门。"""
    adopted, _ = _adopt(_draft(checkpoints=["  "]), item={**_ITEM, "count": 1})

    assert adopted == []


def test_one_call_cannot_beyond_the_per_item_cap() -> None:
    """外部塞一张手写配额表也不能把一次调用变成写小作文。"""
    adopted, _ = _adopt(
        *[_draft(stem=f"第 {index} 道题的题干写得足够长一些") for index in range(9)],
        item={**_ITEM, "count": 9},
    )

    assert len(adopted) == MAX_QUESTIONS_PER_ITEM


def test_duplicate_checkpoints_within_one_question_are_collapsed() -> None:
    """同一题里重复的考察点对评分没有额外信息。"""
    adopted, _ = _adopt(
        _draft(checkpoints=["考察限流", "考察限流", "考察容量规划"]), item={**_ITEM, "count": 1}
    )

    assert adopted[0]["checkpoints"] == ["考察限流", "考察容量规划"]


def test_rows_carry_only_real_columns() -> None:
    """`add_many` 会自己填 `batch` / `order_index`，行里带上它们会撞关键字重复。"""
    adopted, _ = _adopt(_draft(), _draft(), item={**_ITEM, "count": 2})
    row = to_row(adopted[0])

    assert "batch" not in row and "order_index" not in row and "topic" not in row
    assert row["stem"] == adopted[0]["stem"]


def test_ordering_is_by_batch_then_order_index_regardless_of_arrival() -> None:
    """并行分支的到达顺序是调度决定的，题号必须每次一样。"""
    late = {"stem": "a", "batch": 2, "order_index": 0}
    early = {"stem": "b", "batch": 1, "order_index": 3}
    gap = {"stem": "c", "batch": 1, "order_index": 1}

    assert [q["stem"] for q in order_questions([late, gap, early])] == ["c", "b", "a"]
    assert sort_key(gap) < sort_key(late)


def test_planned_count_and_evidence_ratio() -> None:
    items = [{"count": 2}, {"count": 0}, {"count": 3}]
    assert planned_count(items) == 5

    questions = [
        {"source": "resume", "evidence": EVIDENCE},
        {"source": "jd", "evidence": "负责高并发服务的容量规划"},
        {"source": "resume", "evidence": ""},
        {"source": "generic", "evidence": ""},
    ]
    assert evidence_ratio(questions) == 2 / 4
    assert evidence_ratio([]) == 0.0
