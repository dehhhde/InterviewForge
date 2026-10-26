"""出题规划节点：纯确定性的配额表，先攻弱项〔阶段 7 · §10.4.5〕

**这个节点不调 LLM**，所以它不返回 `prompt_version`，`trace` 里那一步的版本号是空的。
理由写在 `docs/adr/0009-m4-deterministic-matching-and-planning.md`：配额表要满足
"count 之和 == total、难度分布与 ratio 误差 <= 1"，而模型只会四舍五入到"看起来差不多"；
`plan` 又是 `generate` 扇出的依据，一次抖动就是十几次调用的成本。

两处确定性保证：
- 难度配额用**最大余数法**（先取整数位，剩的名额按小数位从大到小发，平局按 easy→medium→hard
  的顺序），所以每档与 `ratio * total` 的偏差必然 < 1，且各档之和恰好等于 total；
- 主题铺放按 `profile.focus_topics` 的既有顺序（rubric 已经把它排成弱项在前）**轮流**发牌，
  同档内不比较、不打乱、不读时间，所以同一份 profile 跑两次必然得到同一张表。
"""

from collections.abc import Mapping, Sequence
from typing import Any

from app.agents.state import AgentState
from app.agents.workflow import Node
from app.config import Settings, get_settings
from app.exceptions.base import ValidationFailedError
from app.extractors.base import collapse
from app.services.profile_service import MAX_FOCUS_TOPICS

#: 难度档顺序 = `DifficultyRatio` 的字段顺序。平局时按这个先后发名额，
#: 所以它必须写死而不是从 dict 里赌一次
DIFFICULTY_ORDER: tuple[str, ...] = ("easy", "medium", "hard")

#: 画像没给出任何主题时的兜底槽位名。走到这里说明简历与 JD 都没提供可引的东西，
#: 与其让 total 落空，不如出一档通用题并把这个事实记进 `errors`
_FALLBACK_TOPIC = "综合考察"


def difficulty_quotas(total: int, ratio: Mapping[str, float]) -> dict[str, int]:
    """最大余数法分配各难度档题数。返回按 `DIFFICULTY_ORDER` 排的 dict。

    先按 `floor` 发，剩下的名额给小数部分最大的档——这一步同时保证了两件事：
    各档之和恰好等于 `total`，且任何一档与 `ratio * total` 的偏差不超过 1。
    """
    exact = {name: total * float(ratio.get(name, 0.0)) for name in DIFFICULTY_ORDER}
    quotas = {name: int(exact[name]) for name in DIFFICULTY_ORDER}
    leftover = total - sum(quotas.values())
    # 平局用档名顺序打破（第二键取 `DIFFICULTY_ORDER` 下标），否则剩余名额的归属取决于遍历运气
    ranked = sorted(
        DIFFICULTY_ORDER,
        key=lambda name: (-(exact[name] - quotas[name]), DIFFICULTY_ORDER.index(name)),
    )
    for offset in range(max(leftover, 0)):
        # 循环取而不是切片取：ratio 万一不严格相加为 1（外部传进来的 dict 不受
        # `DifficultyRatio` 校验约束），切片会静默少发几题，循环保证 sum == total
        quotas[ranked[offset % len(ranked)]] += 1
    return quotas


def allocate(
    topics: Sequence[Mapping[str, Any]], quotas: Mapping[str, int]
) -> list[dict[str, Any]]:
    """把每档名额按主题轮流发出去，弱项在前。

    轮流的含义是"第 i 个名额给第 i % len(topics) 个主题"：名额少时只有最弱的几个主题
    拿得到题，名额多时每个主题都拿到、且靠前的主题不比我后的少——这正是"先攻弱项"。
    """
    if not topics:
        return []
    counts: list[dict[str, int]] = [{name: 0 for name in DIFFICULTY_ORDER} for _ in topics]
    for name in DIFFICULTY_ORDER:
        quota = int(quotas.get(name, 0))
        for offset in range(quota):
            counts[offset % len(topics)][name] += 1

    items: list[dict[str, Any]] = []
    for topic, per_topic in zip(topics, counts, strict=True):
        for name in DIFFICULTY_ORDER:
            count = per_topic[name]
            if count <= 0:
                continue
            items.append(
                {
                    "topic": str(topic.get("topic") or _FALLBACK_TOPIC),
                    "difficulty": name,
                    "count": count,
                    "dimension": str(topic.get("dimension") or ""),
                    "source": _item_source(topic),
                    "evidence": topic.get("evidence"),
                    "rationale": str(topic.get("why") or ""),
                }
            )
    return items


def _item_source(topic: Mapping[str, Any]) -> str:
    """`generate` 的 `source` 只有 resume / jd / generic 三档（见 `QuestionSchema`）。"""
    origin = str(topic.get("source") or "")
    if origin == "jd":
        return "jd"
    if origin in ("resume", "rubric"):
        return "resume"
    return "generic"


def make_plan_node(*, settings: Settings | None = None) -> Node:
    """造 plan 节点。它只读 state 与配置，不产生任何副作用。"""
    app_settings = settings or get_settings()

    async def plan_node(state: AgentState) -> dict[str, Any]:
        profile = state.get("profile")
        if not isinstance(profile, Mapping) or not profile.get("dimensions"):
            raise ValidationFailedError(
                "缺少候选人画像，出题配额没有依据",
                code="plan_no_profile",
                details={"resume_id": state.get("resume_id")},
            )

        question = app_settings.question
        errors: list[str] = []
        topics = _topics(profile)
        if not topics:
            topics = [_fallback_topic(profile)]
            errors.append("画像没给出任何主题（技能与缺口都空），按综合考察出题")

        quotas = difficulty_quotas(question.total, question.difficulty_ratio.model_dump())
        items = allocate(topics, quotas)
        plan: dict[str, Any] = {
            "total": question.total,
            "difficulty_quotas": quotas,
            "strategy": "weak_first",
            "topic_count": len(topics),
            "rubric_version": profile.get("rubric_version"),
            "items": items,
        }
        update: dict[str, Any] = {"plan": plan}
        if errors:
            update["errors"] = [f"plan: {note}" for note in errors]
        return update

    return plan_node


def _topics(profile: Mapping[str, Any]) -> list[dict[str, Any]]:
    """取画像给出的主题。形状不对的一律丢掉，别让 `plan.items` 带进半条数据。"""
    raw = profile.get("focus_topics")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        topic = collapse(str(item.get("topic") or ""))
        if len(topic) < 2:
            continue
        out.append(
            {
                "topic": topic,
                "dimension": str(item.get("dimension") or ""),
                "score": int(item.get("score") or 3),
                "why": str(item.get("why") or ""),
                "evidence": item.get("evidence"),
                "source": str(item.get("source") or ""),
            }
        )
    # 画像层已按弱项排过序；这里再排一次只为防御"上游换了实现"，同分则保持原序
    out.sort(key=lambda item: item["score"])
    return out[:MAX_FOCUS_TOPICS]


def _fallback_topic(profile: Mapping[str, Any]) -> dict[str, Any]:
    weakest = _weakest_dimension(profile)
    return {
        "topic": _FALLBACK_TOPIC,
        "dimension": weakest.get("name", ""),
        "score": int(weakest.get("score", 3)),
        "why": f"简历与 JD 都没给出可深挖的主题，围绕{weakest.get('label', '整体表现')}做基础考察",
        "evidence": None,
        "source": "generic",
    }


def _weakest_dimension(profile: Mapping[str, Any]) -> Mapping[str, Any]:
    dims = [item for item in (profile.get("dimensions") or []) if isinstance(item, Mapping)]
    if not dims:
        return {"name": "", "label": "整体表现", "score": 3}
    return min(dims, key=lambda item: (int(item.get("score", 5)), str(item.get("name", ""))))
