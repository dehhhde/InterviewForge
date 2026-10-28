"""命令行导出：前缀解析、旧行扫描兜底、"新行绝不扫库"〔S3 · T-302 + T-304〕

端点那条路在 `tests/test_api/test_export_api.py`（那边钉的是下载外壳与内容一致）。
这里只钉脚本自己**多做**的两件事，因为它们都不在 HTTP 路径上：

1. id 前缀解析与唯一性检查（人打字不方便，端点不必供这个能力）；
2. `thread_id` 为空的历史报告：扫全部线程按 `interview_id` 认回来。
   这条是 T-304 的另一半——新报告有可寻址线程，所以**不许**再扫（用例把扫描函数
   换成"一调就炸"来钉住它），旧报告没有，只能扫。

`CHECKPOINT_DB` 被指到 `tmp_path`：`data/checkpoints.db` 是这台机器上真跑过的报告，
测试读它就是把用例和开发历史耦合起来。库同理，靠 autouse 把 `database_url` 换掉，
所以必须走 `get_sessionmaker()` 的脚本才测得到。
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from app.agents import workflow
from app.agents.workflow import build_agent_graph, make_thread_config, open_checkpointer
from app.config import get_settings
from app.dependencies import dispose_engine, get_engine, get_sessionmaker
from app.models import Base
from app.repositories.interview_repository import InterviewRepository
from app.repositories.job_repository import JobRepository
from app.repositories.question_repository import QuestionRepository
from app.repositories.resume_repository import ResumeRepository
from app.services import export_service
from app.utils.db import session_scope
from scripts.export_report_md import main
from tests.test_agent_infra import _nodes

_PLAN = {
    "total": 3,
    "difficulty_quotas": {"easy": 1, "medium": 1, "hard": 2},
    "strategy": "weak_first",
    "topic_count": 1,
    "items": [
        {
            "topic": "限流",
            "difficulty": "medium",
            "count": 3,
            "source": "resume",
            "rationale": "简历提到令牌桶",
        }
    ],
}


@pytest.fixture(autouse=True)
async def _isolated_databases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """业务库与 checkpoint 库都指到本用例的 `tmp_path`，并建好表。"""
    settings = get_settings()
    monkeypatch.setattr(
        settings, "database_url", f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    )
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    monkeypatch.setattr(workflow, "CHECKPOINT_DB", tmp_path / "data" / "checkpoints.db")

    await dispose_engine()
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield
    await dispose_engine()


async def _seed_report(
    *, thread_id: str, name: str = "林可", job_title: str = "后端工程师"
) -> str:
    async with session_scope(get_sessionmaker()) as session:
        resume = await ResumeRepository(session).create(
            name=name, raw_text=f"{name} 的简历原文", file_name=f"{name}.pdf"
        )
        job = await JobRepository(session).create(
            title=job_title, company="示例公司", jd_text="掌握 MySQL 索引优化", required_skills=[]
        )
        head = await InterviewRepository(session).create(
            resume_id=resume.id,
            job_id=job.id,
            plan={},
            status="completed",
            thread_id=thread_id,
            resume_name=name,
            job_title=job_title,
            total_questions=1,
        )
        await QuestionRepository(session).add_many(
            [
                {
                    "stem": "令牌桶和漏桶的差别在哪",
                    "difficulty": "medium",
                    "source": "resume",
                    "reference_answer": "突发容忍与恒定流速的取舍",
                    "checkpoints": ["说清两者适用场景"],
                    "rubric": ["能对比即满分"],
                    "follow_ups": [],
                    "evidence": "简历：引入令牌桶限流",
                }
            ],
            interview_id=head.id,
            batch=1,
        )
        return head.id


async def _write_checkpoint(interview_id: str, thread: str) -> None:
    """真跑一次图（假节点），把这份报告的图终态留在 checkpoint 库里。"""

    async def write_ids(state: dict[str, Any]) -> dict[str, Any]:
        return {"interview_id": interview_id}

    async def write_plan(state: dict[str, Any]) -> dict[str, Any]:
        return {"plan": _PLAN}

    async with open_checkpointer(workflow.CHECKPOINT_DB) as saver:
        graph = build_agent_graph(_nodes(parse=write_ids, plan=write_plan), checkpointer=saver)
        await graph.ainvoke({}, make_thread_config(thread, recursion_limit=30))


def _argv(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["export_report_md.py", *args])


# --- 命令行外壳 ---


async def test_missing_id_prints_usage_and_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    _argv(monkeypatch)
    assert await main() == 2


async def test_prefix_must_exist_and_be_unique(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "exported.md"
    first = await _seed_report(thread_id="")
    await _seed_report(thread_id="", job_title="数据工程师")

    _argv(monkeypatch, "no-such-prefix", str(out))
    assert await main() == 1

    # 空前缀命中全部报告：与其猜一份，不如让人把 id 补全
    _argv(monkeypatch, "", str(out))
    assert await main() == 1
    assert not out.exists()

    # 认得出前缀就认（人打字不方便），但完整 id 永远是最省事的写法
    _argv(monkeypatch, first, str(out))
    assert await main() == 0
    assert "令牌桶" in out.read_text(encoding="utf-8")


# --- 新报告：定位单线程，绝不扫库 ---


async def test_a_thread_addressable_report_is_read_directly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """T-304 的验收落在脚本上：有 `thread_id` 时扫描那一步根本不该发生。"""
    interview_id = await _seed_report(thread_id="thread-new")
    await _write_checkpoint(interview_id, "thread-new")

    def explode() -> list[str]:
        raise AssertionError("有可寻址线程还去扫全部线程")

    monkeypatch.setattr("scripts.export_report_md._thread_ids", explode)

    out = tmp_path / "new.md"
    _argv(monkeypatch, interview_id, str(out))
    assert await main() == 0

    text = out.read_text(encoding="utf-8")
    # 配额表那一节只有图终态里有：它出现即证明 state 读到了
    assert "目标题数 **3**" in text
    assert export_service.NO_STATE_NOTE not in text
    assert "令牌桶和漏桶的差别在哪" in text


# --- 旧报告：按 interview_id 认回来 ---


async def test_a_legacy_report_recovers_its_state_by_scanning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    interview_id = await _seed_report(thread_id="")
    await _write_checkpoint(interview_id, "thread-old")
    await _write_checkpoint("iv-somebody-else", "thread-noise")

    out = tmp_path / "old.md"
    _argv(monkeypatch, interview_id, str(out))
    assert await main() == 0

    text = out.read_text(encoding="utf-8")
    assert "目标题数 **3**" in text
    # 认的是 `interview_id`，不是"最近一次运行"：别的线程不该串进来
    assert "iv-somebody-else" not in text


async def test_a_report_whose_checkpoint_is_gone_still_exports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """checkpoint 被清理过（T-311 之后这是常态）：导出仍要有题目，并说明缺了哪几节。"""
    interview_id = await _seed_report(thread_id="thread-swept")

    out = tmp_path / "swept.md"
    _argv(monkeypatch, interview_id, str(out))
    assert await main() == 0

    text = out.read_text(encoding="utf-8")
    assert export_service.NO_STATE_NOTE in text
    assert "令牌桶和漏桶的差别在哪" in text
    assert "### 1.1 五维评分" not in text
