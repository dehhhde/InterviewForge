"""`GET /interviews/{id}/export.md`：把报告导成 Markdown 下载〔S3 · T-302 / OPT-6.2〕

这里钉的是**一个渲染出处**这件事：端点、`scripts/export_report_md.py` 与将来的打印入口
都调 `export_service.render_markdown`，所以"同名不同内容"的两套分叉（BACKLOG P3-10 的账）
不可能再长回来。断言因此分三类：

1. HTTP 外壳——状态码、内容类型、`Content-Disposition` 的编码与消毒（文件名是用户输入的
   简历名与岗位名，换行能切开头，必须挡）；
2. 内容与业务行一致——题目来自 `question` 表最新批次，不是图终态里的草稿；
3. **只读自己那一个线程**〔T-304〕：`interview.thread_id` 是图终态的唯一寻址方式，
   端点不许扫 checkpoint 线程表（那是脚本对旧行的兜底活）。

`read_thread_state` 一律被替身换掉：真 `data/checkpoints.db` 是开发库，测试碰它就是把
"跑测试"和"这台机器上跑过哪些报告"耦合起来。图终态的读法本身在 `tests/test_agent_infra.py`
用 `tmp_path` 的 checkpoint 测。断言只落在结构与章节有无上，从不钉渲染文案的措辞。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote

import pytest
from httpx import AsyncClient

from app.dependencies import get_sessionmaker
from app.repositories.interview_repository import InterviewRepository
from app.repositories.job_repository import JobRepository
from app.repositories.question_repository import QuestionRepository
from app.repositories.resume_repository import ResumeRepository
from app.services import export_service
from app.utils.db import session_scope

#: `quote(safe="")` 之后还可能出现在线上的字符：RFC 3986 的未保留集加百分号。
_RFC5987_SAFE = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~%"
)

_QUESTIONS = [
    {
        "stem": "令牌桶和漏桶的差别在哪",
        "difficulty": "medium",
        "source": "resume",
        "reference_answer": "突发容忍与恒定流速的取舍",
        "checkpoints": ["说清两者适用的场景"],
        "rubric": ["能对比即满分"],
        "follow_ups": ["那限流阈值怎么定"],
        "evidence": "简历：引入令牌桶限流",
    },
    {
        "stem": "MySQL 索引什么时候会失效",
        "difficulty": "hard",
        "source": "jd",
        "reference_answer": "最左前缀、函数包裹、隐式转换",
        "checkpoints": ["答出三类失效路径"],
        "rubric": ["有实测意识加分"],
        "follow_ups": ["怎么确认走了索引"],
        "evidence": "JD：掌握 MySQL 索引优化",
    },
    {
        "stem": "讲讲你最近一次线上排障",
        "difficulty": "easy",
        "source": "generic",
        "reference_answer": "定位—止血—复盘三步",
        "checkpoints": ["有具体数字"],
        "rubric": ["能复盘即通过"],
        "follow_ups": [],
        "evidence": "",
    },
]


def _state(interview_id: str) -> dict[str, Any]:
    """一次运行的图终态——只放渲染要读的那几个通道，形状照 `AgentState`。"""
    return {
        "interview_id": interview_id,
        "parsed": {"name": "林可", "years_experience": 5, "skills": [{"name": "Python"}]},
        "profile": {
            "rubric_version": "r1",
            "confidence": 0.7,
            "summary": "后端主线清楚，高并发证据只有项目自述。",
            "strengths": ["分布式项目有落地"],
            "weaknesses": ["缺压测数据"],
            "dimensions": [
                {
                    "name": "depth",
                    "label": "技术深度",
                    "score": 62,
                    "evidence": "项目里有中间件实践",
                    "signals": {"projects": 2},
                }
            ],
            "focus_topics": [
                {"topic": "限流", "dimension": "depth", "score": 40, "why": "只有一句自述"}
            ],
        },
        "plan": {
            "total": 3,
            "difficulty_quotas": {"easy": 1, "medium": 1, "hard": 1},
            "strategy": "weak_first",
            "topic_count": 2,
            "items": [
                {
                    "topic": "限流",
                    "difficulty": "medium",
                    "count": 1,
                    "source": "resume",
                    "rationale": "简历提到令牌桶",
                }
            ],
        },
        "matched_gaps": [
            {
                "requirement": "MySQL 索引优化",
                "level": "partial",
                "skill": "mysql",
                "source": "dict",
                "evidence": "简历：慢查询治理",
            }
        ],
        "questions": [
            {"order_index": 0, "topic": "限流"},
            {"order_index": 1, "topic": "索引"},
            {"order_index": 2, "topic": "排障"},
        ],
        "trace": [{"node": "parse", "ms": 812.4, "ok": True, "prompt_version": "v2"}],
        "validate_retries": 0,
    }


async def _seed_report(
    *,
    name: str = "林可",
    job_title: str = "后端工程师",
    thread_id: str = "a" * 32,
    status: str = "completed",
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
            status=status,
            thread_id=thread_id,
            resume_name=name,
            job_title=job_title,
            total_questions=len(_QUESTIONS) if status == "completed" else 0,
        )
        if status == "completed":
            await QuestionRepository(session).add_many(
                _QUESTIONS, interview_id=head.id, batch=1
            )
        return head.id


@pytest.fixture(autouse=True)
def _no_real_checkpoint_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """端点默认会按 `thread_id` 去读 checkpoint 库——这里换成"读不到"，别碰 `data/`。"""

    async def nothing(thread_id: str, **kwargs: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(export_service, "read_thread_state", nothing)


def _inject_state(
    monkeypatch: pytest.MonkeyPatch, state: dict[str, Any]
) -> list[dict[str, Any]]:
    """把图终态换成给定内容，并记下**收到过哪些线程 id**（T-304 的接缝就在这）。"""
    seen: list[dict[str, Any]] = []

    async def fake(thread_id: str, **kwargs: Any) -> dict[str, Any]:
        seen.append({"thread_id": thread_id})
        return state

    monkeypatch.setattr(export_service, "read_thread_state", fake)
    return seen


# --- 下载外壳 ---


async def test_export_gives_markdown_and_a_encoded_download_name(client: AsyncClient) -> None:
    interview_id = await _seed_report()

    response = await client.get(f"/api/v1/interviews/{interview_id}/export.md")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    # 中文名只能走 RFC 5987 那一段；ASCII 的 `filename=` 是不认 5987 的老客户端回退。
    assert 'filename="report.md"' in disposition
    encoded = disposition.split("filename*=UTF-8''", 1)[1]
    assert unquote(encoded, "utf-8") == "面试题-林可-后端工程师.md"


async def test_a_hostile_file_name_cannot_break_the_header(client: AsyncClient) -> None:
    """简历名与岗位名都是用户输入：换行会切开响应头，引号会破坏 `filename="…"`。"""
    interview_id = await _seed_report(name='甲\r\nX-Evil: 1"', job_title='乙";/x')

    response = await client.get(f"/api/v1/interviews/{interview_id}/export.md")

    disposition = response.headers["content-disposition"]
    encoded = disposition.split("filename*=UTF-8''", 1)[1]
    # 上线那一段只剩"未保留字符 + `%XX`"：引号、分号、斜杠、换行全被编进去了，
    # 客户端解析这一个头时不可能被文件名带着走。
    assert set(encoded) <= _RFC5987_SAFE
    # ASCII 兜底那一份是**固定的** `report.md`：`filename="…"` 的引号结构里从不装用户输入
    assert 'filename="report.md"' in disposition
    # 换行在编码**之前**就被剥掉了：解码回来也不该有，剩下的字符只是普通文件名内容
    assert unquote(encoded, "utf-8") == "面试题-甲X-Evil: 1-乙;/x.md"


async def test_unknown_report_is_404_with_the_shared_envelope(client: AsyncClient) -> None:
    response = await client.get("/api/v1/interviews/nope/export.md")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["details"] == {"interview_id": "nope"}


# --- 内容：业务行为准，图终态补齐 ---


async def test_export_lists_the_persisted_questions(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    interview_id = await _seed_report()
    _inject_state(monkeypatch, _state(interview_id))

    text = (await client.get(f"/api/v1/interviews/{interview_id}/export.md")).text

    for question in _QUESTIONS:
        assert question["stem"] in text
    # 章节齐全：简历 / 五维 / JD 逐条判定 / 配额表 / 题目 / 批注 / 观测
    for heading in (
        "## 1. 简历分析",
        "### 1.1 五维评分",
        "### 1.3 JD 要求逐条判定",
        "## 2. 出题配额表",
        "## 3. 面试题（3 道）",
        "## 4. 评审批注",
        "## 5. 运行观测",
    ):
        assert heading in text
    assert "第 1 题" in text and "限流" in text
    # 通用题没有依据是事实，渲染成"不绑定原文"而不是编一条出处
    assert "通用题，不绑定原文" in text
    # 回溯比例由代码算，不靠模型自述：2/3 带 evidence
    assert "67%" in text


async def test_endpoint_and_service_give_the_same_bytes(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一个渲染处出两份调用：端点与脚本导出的必须是同一份内容。

    比对的是"同样的输入 → 同样的输出"，不是两份文件的路径：脚本那条路还会做 id 前缀解析
    与旧行扫描，那些差异不该出现在这份断言里。
    """
    interview_id = await _seed_report()
    state = _state(interview_id)
    _inject_state(monkeypatch, state)

    response = await client.get(f"/api/v1/interviews/{interview_id}/export.md")

    async with session_scope(get_sessionmaker()) as session:
        context = await export_service.collect_context(session, interview_id)
    assert context is not None
    assert response.text == export_service.render_markdown(context)


async def test_export_reads_only_its_own_thread(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-304 的验收：一次定位，不是一次遍历。"""
    interview_id = await _seed_report(thread_id="b" * 32)
    seen = _inject_state(monkeypatch, _state(interview_id))

    await client.get(f"/api/v1/interviews/{interview_id}/export.md")

    assert seen == [{"thread_id": "b" * 32}]


async def test_legacy_report_without_thread_id_exports_questions_only(client: AsyncClient) -> None:
    """旧报告那一列是空串：缺的章节整段不渲染，并在文首说明缺了什么。

    这条用例**不打替身**——`thread_id` 为空时 `read_thread_state` 直接返回空 dict，
    连 checkpoint 库都不该打开。跑在真应用上却没碰 `data/`，正是这个短路在保护。
    """
    interview_id = await _seed_report(thread_id="")

    text = (await client.get(f"/api/v1/interviews/{interview_id}/export.md")).text

    assert export_service.NO_STATE_NOTE in text
    assert "MySQL 索引什么时候会失效" in text
    for heading in ("### 1.1 五维评分", "### 1.3 JD 要求逐条判定", "| 节点 | 耗时 |"):
        assert heading not in text
    # 主题在 `question` 表里没有列，图终态缺席时只能留破折号，不编一个主题名
    assert "｜—" in text


async def test_running_report_still_downloads_with_no_questions(client: AsyncClient) -> None:
    """跑中途请求导出：合法响应是一份"0 题"的文件，不是 404、也不是半份草稿。

    `review` 之前题目本来就不在库里，所以这里诚实显示 0 道；空报告的解释归前端按
    `status` 分支（ADR-0015）。
    """
    interview_id = await _seed_report(status="running", thread_id="")

    response = await client.get(f"/api/v1/interviews/{interview_id}/export.md")

    assert response.status_code == 200
    assert "## 3. 面试题（0 道）" in response.text
    assert "**running**" in response.text
