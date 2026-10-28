"""离线量扇出 prompt 的体积构成〔T-205 的基线尺〕：一次模型都不花，只数字符。

为什么不用 `scripts/eval_e2e.py`：它报的是真调的 `prompt_tokens`，而真调卡在决策 D-2
（账户余额）。载荷瘦身改的是"每个分支重发一遍同样的上下文"，这件事完全能在离线侧量——
把整条确定性链跑通（抽取 → 比对 → 画像 → 配额），再用**生产同一个构造点**
（`workflow.route_after_generate`）扇出，逐槽位渲染出题 prompt 数字符。

字符数只是量级不是账单：中文词表大约 1.5 字/token，除出来的数用来说"改前改后省了几成"，
不用来对账。真调验收（`带可回溯引用比例` 不许掉）仍要等 D-2。

用法：`PYTHONPATH=. .venv/Scripts/python.exe -X utf8 scripts/probe_fanout_payload.py`
"""

import asyncio
import json
from pathlib import Path
from typing import Any

from app.agents.nodes.generate_node import _messages
from app.agents.nodes.plan_node import make_plan_node
from app.agents.workflow import route_after_generate
from app.config import get_settings
from app.extractors.ner_extractor import extract_by_regex
from app.extractors.project_extractor import extract_projects
from app.extractors.skill_extractor import extract_skills
from app.extractors.timeline_extractor import extract_timeline
from app.parsers.pdf_parser import PdfParser
from app.prompts.registry import get_prompt
from app.services.match_service import build_match
from app.services.profile_service import (
    confidence_from_signals,
    derive_focus_topics,
    fallback_narrative,
    score_dimensions,
)

REAL_RESUME = Path("tests/fixtures/resumes/附件简历-梁清生.pdf")
#: 与 `app/scripts/seed.py` 的第一份岗位同文（种子脚本要连库，不适合被这里 import）
JD_TEXT = (
    "岗位职责：负责核心业务的后端服务设计与实现。\n"
    "任职要求：精通 Java，熟悉 Spring Boot；掌握 MySQL 的索引与事务；"
    "了解 Redis 缓存设计；有分布式经验优先。"
)


def _offline_parsed(raw_text: str) -> dict[str, Any]:
    """只走确定性抽取器装配 `parsed`：这条链在这里一个字都不该花钱。"""
    fields: dict[str, Any] = {}
    fields.update(extract_by_regex(raw_text).as_resume_fields())
    fields.update(extract_timeline(raw_text).as_resume_fields())
    fields.update(extract_skills(raw_text).as_resume_fields())
    fields.update(extract_projects(raw_text).as_resume_fields())
    return fields


def _state() -> dict[str, Any]:
    raw_text = PdfParser().parse(REAL_RESUME).raw_text
    parsed = _offline_parsed(raw_text)
    gaps = list(build_match(JD_TEXT, parsed).gaps)
    dimensions = score_dimensions(parsed, gaps)
    topics = derive_focus_topics(parsed, gaps, dimensions)
    confidence = confidence_from_signals(parsed, gaps)
    profile = {
        "candidate": {
            "name": str(parsed.get("name") or ""),
            "years_experience": parsed.get("years_experience"),
        },
        "dimensions": [dimension.as_dict() for dimension in dimensions],
        "focus_topics": topics,
        **fallback_narrative(parsed, dimensions, topics, confidence),
    }
    return {
        "raw_text": raw_text,
        "jd_text": JD_TEXT,
        "parsed": parsed,
        "matched_gaps": gaps,
        "profile": profile,
        # 批次号由 `generate` 节点自己 +1，路由只读它，所以探针要手工给一个
        "question_batch": 1,
    }


def _sizes(job: dict[str, Any]) -> dict[str, int]:
    """一个分支的 prompt 总字数，以及三段可省上下各自的字数。"""
    prompt = get_prompt("generate")
    item = job.get("plan_item") or {}
    chars = sum(
        len(message["content"])
        for message in _messages(prompt, job, count=int(item.get("count") or 0))
    )
    return {
        "prompt": chars,
        "parsed": len(json.dumps(job.get("parsed") or {}, ensure_ascii=False, default=str)),
        "jd": len(str(job.get("jd_text") or "")),
        "roster": len(json.dumps(job.get("roster") or [], ensure_ascii=False, default=str)),
        "raw": len(str(job.get("raw_text") or "")),
    }


async def _measure() -> None:
    state = _state()
    state["plan"] = (await make_plan_node()(state))["plan"]
    sends = route_after_generate(state)
    if isinstance(sends, str):
        raise SystemExit("扇出被跳过了（批次号为 0 或槽位为空），量不到载荷")

    per_source: dict[str, dict[str, int]] = {}
    total_prompt = 0
    for send in sends:
        job = send.arg
        source = str((job.get("plan_item") or {}).get("source") or "generic")
        sizes = _sizes(job)
        total_prompt += sizes["prompt"]
        bucket = per_source.setdefault(source, {"slots": 0, **dict.fromkeys(sizes, 0)})
        bucket["slots"] += 1
        for key, value in sizes.items():
            bucket[key] += value

    settings = get_settings()
    print(f"真简历：{REAL_RESUME.name}；岗位 JD：{len(JD_TEXT)} 字")
    print(
        f"扇出 {len(sends)} 个分支（`question.total={settings.question.total}`），"
        f"出题 prompt 字符合计 {total_prompt:,}"
    )
    print(f"按 1.5 字/token 粗估约 {total_prompt * 2 // 3:,} token（只用于比较改前改后）")
    print()
    columns = ("source", "槽位", "prompt", "parsed", "jd", "roster", "raw_text")
    widths = (8, 4, 10, 10, 8, 8, 10)
    print("  ".join(f"{name:>{width}}" for name, width in zip(columns, widths)))
    for source, bucket in sorted(per_source.items()):
        row = (
            source,
            str(bucket["slots"]),
            f"{bucket['prompt']:,}",
            f"{bucket['parsed']:,}",
            f"{bucket['jd']:,}",
            f"{bucket['roster']:,}",
            f"{bucket['raw']:,}",
        )
        print("  ".join(f"{value:>{width}}" for value, width in zip(row, widths)))
    print()
    print("口径：`prompt` 是渲染后的 system+user 字数；`parsed`/`jd`/`roster` 是这三段")
    print("在**全部槽位**上重复发送的字数（瘦身的上限就是它们），`raw_text` 不进 prompt，")
    print("只占 checkpoint 里的载荷。")


if __name__ == "__main__":
    asyncio.run(_measure())
