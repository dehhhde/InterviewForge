"""plan 节点的配额测试：验收的两条硬指标 + 确定性（全程离线，不调模型）。

这里锁的是计划书 M4 的验收：`plan.items` 的 count 之和 == `settings.question.total`，
难度分布与 `difficulty_ratio` 误差 <= 1 题，以及"同一 profile 两次结果一致"。
"""

import json
from typing import Any

import pytest

from app.agents.nodes.plan_node import (
    ANGLES_BY_DIFFICULTY,
    DIFFICULTY_ORDER,
    allocate,
    difficulty_quotas,
    make_plan_node,
)
from app.config import DifficultyRatio, QuestionParams, Settings, get_settings
from app.exceptions.base import ValidationFailedError

_TOTAL = 15
_RATIO = (0.3, 0.5, 0.2)


def _settings(total: int = _TOTAL, ratio: tuple[float, float, float] = _RATIO) -> Settings:
    """深拷贝一份配置再改数量与配比。

    必须拷贝：`get_settings()` 是 `lru_cache` 单例，直接改它会污染同进程的其他用例
    （AGENTS §6 说"用 monkeypatch 换，别改 .env"，这里同理——别改单例）。
    """
    copy = get_settings().model_copy(deep=True)
    copy.question = QuestionParams(
        total=total,
        difficulty_ratio=DifficultyRatio(easy=ratio[0], medium=ratio[1], hard=ratio[2]),
    )
    return copy


def _topic(name: str, score: int, source: str = "jd") -> dict[str, Any]:
    return {
        "topic": name,
        "dimension": "job_fit",
        "score": score,
        "why": f"{name} 是弱项",
        "evidence": None,
        "source": source,
    }


def _profile(topics: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "rubric_version": "v1",
        "candidate": {"name": "林可", "years_experience": 3.0},
        "dimensions": [
            {"name": "skill_breadth", "label": "技能广度", "score": 2},
            {"name": "job_fit", "label": "岗位匹配度", "score": 1},
        ],
        "focus_topics": topics,
    }


@pytest.mark.parametrize("total", [1, 2, 3, 7, 15, 17, 40])
@pytest.mark.parametrize(
    "ratio",
    [
        (0.3, 0.5, 0.2),
        (0.34, 0.33, 0.33),
        (1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0),
        (0.5, 0.0, 0.5),
    ],
)
def test_quotas_sum_to_total_and_stay_within_one_of_the_ratio(
    total: int, ratio: tuple[float, float, float]
) -> None:
    """最大余数法的两条数学保证——验收项就在这里，与主题内容无关。"""
    quotas = difficulty_quotas(total, dict(zip(DIFFICULTY_ORDER, ratio, strict=True)))

    assert sum(quotas.values()) == total
    assert list(quotas) == list(DIFFICULTY_ORDER)
    for name, share in zip(DIFFICULTY_ORDER, ratio, strict=True):
        assert abs(quotas[name] - total * share) <= 1


async def test_plan_items_sum_to_settings_total_and_reproduce_the_distribution() -> None:
    node = make_plan_node(settings=_settings())

    update = await node({"profile": _profile([_topic("MySQL", 1), _topic("Redis", 2)])})

    plan = update["plan"]
    items = plan["items"]
    assert sum(item["count"] for item in items) == _TOTAL
    per_difficulty = {name: 0 for name in DIFFICULTY_ORDER}
    for item in items:
        per_difficulty[item["difficulty"]] += item["count"]
    for count, share in zip(per_difficulty.values(), _RATIO, strict=True):
        assert abs(count - _TOTAL * share) <= 1
    assert plan["total"] == _TOTAL
    assert plan["strategy"] == "weak_first"
    assert plan["rubric_version"] == "v1"


async def test_the_same_profile_always_yields_the_same_plan() -> None:
    """M4 的确定性要求：`plan` 是纯函数，重跑不引入任何抖动。"""
    profile = _profile([_topic(n, i) for i, n in enumerate(["MySQL", "Redis", "Kafka"], start=1)])
    node = make_plan_node(settings=_settings())

    first, second = await node({"profile": profile}), await node({"profile": profile})

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


async def test_plan_never_touches_the_model() -> None:
    """节点不接受 `llm`：配额表要确定性，而 DeepSeek 没有 seed 参数可调。

    断言方式是"构造签名里没有注入点"——比读一遍源码更硬，加了参数这条就红。
    """
    with pytest.raises(TypeError, match="unexpected keyword"):
        make_plan_node(llm=object())  # type: ignore[call-arg]


async def test_a_small_budget_only_buys_the_weakest_topic() -> None:
    """名额比主题少时，题必须全给最弱的那一项——这就是"先攻弱项"的全部含义。"""
    node = make_plan_node(settings=_settings(total=3))

    update = await node(
        {"profile": _profile([_topic("MySQL", 1), _topic("Redis", 2), _topic("Java", 4)])}
    )

    items = update["plan"]["items"]
    assert {item["topic"] for item in items} == {"MySQL"}
    assert sum(item["count"] for item in items) == 3


async def test_every_weak_topic_gets_at_least_one_slot_when_budget_allows() -> None:
    """total >= 主题数时，每个弱项主题至少一槽——`generate` 的扇出下限由此保证。"""
    topics = [_topic(f"技能{i}", score) for i, score in enumerate([1, 1, 2, 2, 3], start=1)]
    node = make_plan_node(settings=_settings(total=15))

    update = await node({"profile": _profile(topics)})

    covered = {item["topic"] for item in update["plan"]["items"]}
    assert covered == {topic["topic"] for topic in topics}
    for topic in topics:
        total_for_topic = sum(
            item["count"] for item in update["plan"]["items"] if item["topic"] == topic["topic"]
        )
        assert total_for_topic >= 1


async def test_item_shape_carries_the_provenance_generate_needs() -> None:
    node = make_plan_node(settings=_settings())

    update = await node({"profile": _profile([_topic("MySQL", 1, source="resume")])})

    for item in update["plan"]["items"]:
        assert item["difficulty"] in DIFFICULTY_ORDER
        assert item["source"] in ("resume", "jd", "generic")
        assert item["count"] >= 1
        assert item["topic"] == "MySQL"
        assert item["dimension"] == "job_fit"
        assert item["rationale"]


async def test_angles_are_part_of_the_provenance_generate_needs() -> None:
    """每个槽位带 `angles`，长度恰好等于题数、且全部取自本档的角度池〔T-202〕。

    长度相等是因为题数由配额定死（`sum(count) == total` 那条验收不能因为加角度而变），
    取自本档是因为角度必须与难度相配——让 hard 槽位去问"名词与定义"，配额表就名存实亡。
    """
    node = make_plan_node(settings=_settings())

    update = await node({"profile": _profile([_topic("MySQL", 1, source="resume")])})

    for item in update["plan"]["items"]:
        pool = ANGLES_BY_DIFFICULTY[item["difficulty"]]
        assert len(item["angles"]) == item["count"]
        assert set(item["angles"]) <= set(pool)
        if item["count"] <= len(pool):
            assert len(set(item["angles"])) == item["count"], "没超过池长时不许两个题同一个角度"


def test_one_topic_three_difficulties_get_three_different_angles() -> None:
    """这张卡要修的就是那一幕：同主题三档各写一遍"那道最经典的题"（实测 3/4 批注是换皮重复）。

    用 `allocate` 直接喂 1/1/1 而不是走整个节点，是为了把形状钉在最窄的地方——
    三个槽位、一题一档，角度仍必须互不相同。
    """
    items = allocate([_topic("MySQL", 1)], {"easy": 1, "medium": 1, "hard": 1})

    assert [item["difficulty"] for item in items] == list(DIFFICULTY_ORDER)
    first_angles = [item["angles"][0] for item in items]
    assert len(set(first_angles)) == 3, first_angles


def test_a_slot_bigger_than_the_pool_wraps_instead_of_running_dry() -> None:
    """池用完取模，而不是给到第 4 题就没角度〔T-202〕。

    取模是有意的兜底：`question_service.MAX_QUESTIONS_PER_ITEM` 封在 6，所以最坏情况
    是第 5、6 题与第 1、2 题同角度——那时该扩池，而不是让 `len(angles) != count` 成立。
    """
    items = allocate([_topic("MySQL", 1)], {"easy": 0, "medium": 6, "hard": 0})
    angles = items[0]["angles"]

    pool = list(ANGLES_BY_DIFFICULTY["medium"])
    assert len(angles) == 6 == items[0]["count"]
    assert angles[: len(pool)] == pool
    assert angles[len(pool) :] == pool[: len(angles) - len(pool)]


async def test_angles_are_deterministic_like_the_rest_of_the_plan() -> None:
    """ADR-0009 的确定性验收现在覆盖角度：同一份 profile 两次跑出逐字节相同的表。"""
    profile = _profile([_topic(n, i) for i, n in enumerate(["MySQL", "Redis", "Kafka"], start=1)])
    node = make_plan_node(settings=_settings())

    first, second = await node({"profile": profile}), await node({"profile": profile})

    assert [item["angles"] for item in first["plan"]["items"]] == [
        item["angles"] for item in second["plan"]["items"]
    ]


async def test_jd_sourced_topics_keep_source_jd_and_rubric_ones_map_to_resume() -> None:
    """`QuestionSchema.source` 只有三档：JD 缺口算 jd，画像维度/简历技能算 resume。"""
    profile = _profile([_topic("MySQL", 1, source="jd"), _topic("技能广度", 2, source="rubric")])
    node = make_plan_node(settings=_settings())

    update = await node({"profile": profile})

    by_topic: dict[str, str] = {}
    for item in update["plan"]["items"]:
        by_topic.setdefault(item["topic"], item["source"])
    assert by_topic == {"MySQL": "jd", "技能广度": "resume"}


async def test_missing_profile_is_a_failure_not_an_empty_plan() -> None:
    """没有画像就没有配额依据。返回空 items 会让 review 误判"这套题很干净"。"""
    node = make_plan_node(settings=_settings())

    for state in ({}, {"profile": {"focus_topics": []}}):
        with pytest.raises(ValidationFailedError) as caught:
            await node(state)
        assert caught.value.code == "plan_no_profile"


async def test_an_empty_topic_list_degrades_to_one_generic_slot_with_a_note() -> None:
    """画像没给出主题时仍要出满 total，并把这件事记进 errors 通道。"""
    profile = _profile([])
    node = make_plan_node(settings=_settings())

    update = await node({"profile": profile})

    assert sum(item["count"] for item in update["plan"]["items"]) == _TOTAL
    assert {item["source"] for item in update["plan"]["items"]} == {"generic"}
    assert update["errors"][0].startswith("plan: ")


async def test_malformed_topics_are_dropped_instead_of_half_read() -> None:
    node = make_plan_node(settings=_settings())

    update = await node(
        {
            "profile": _profile(
                [
                    {"topic": "  "},
                    {"topic": "MySQL", "score": 1},
                    "不是对象的一条",
                ]
            )
        }
    )

    assert {item["topic"] for item in update["plan"]["items"]} == {"MySQL"}


async def test_only_the_first_max_focus_topics_are_used() -> None:
    """主题多于画像层的上限时，多余的主题不参与铺槽（它们本来也不该出现）。"""
    from app.services.profile_service import MAX_FOCUS_TOPICS

    topics = [_topic(f"技能{i}", 1) for i in range(MAX_FOCUS_TOPICS + 4)]
    node = make_plan_node(settings=_settings())

    update = await node({"profile": _profile(topics)})

    assert update["plan"]["topic_count"] == MAX_FOCUS_TOPICS


def test_allocate_with_no_topics_returns_nothing() -> None:
    assert allocate([], {"easy": 1, "medium": 1, "hard": 1}) == []


async def test_plan_does_not_write_any_channel_but_its_own() -> None:
    """只返回增量：写别的通道会冲掉上游结果。"""
    node = make_plan_node(settings=_settings())

    update = await node({"profile": _profile([_topic("MySQL", 1)])})

    assert set(update) == {"plan"}
