r"""列表行的字段投影〔S3 · T-301 / OPT-6.1〕

三张列表端点原本发的是**详情 DTO**：`GET /resumes/list` 每行带整份 `parsed` JSON、
`GET /jobs` 每行带整篇 `jd_text`、`GET /interviews/list` 带一个恒为空的 `questions`。
这张卡要钉住四件事：

1. **正文列不出现在列表行里**。它只要出现在列表响应里，就会被下一个读代码的人当成
   "列表已经给了"，投影随时能被悄悄改回去；
2. **详情端点照旧给全文**。投影不是砍功能：列表少发的字节必须由一条读得到的路补上，
   否则前端只能把正文塞回列表——那就是原地打转；
3. **列表行与详情行共用同一个核心模型**（"别拆两份 DTO 真相"的机器护栏）。两份平行
   模型不会报错，只会让列名各改一半，所以护栏必须是结构断言而不是"看起来一样"；
4. **列表载荷真的比详情小**：这是卡片承诺的收益（几百 KB → 几 KB），没有断言它就会在
   某次"顺手加回一列"里蒸发。

概览的三个数与 JD 摘要都是**从正文列现算的**（表里没有副本列），所以断言它们等于
"从 `parsed` / `jd_text` 直接算出来的那一份"，而不是某个写死的字面量。
"""

from __future__ import annotations

import json
from typing import Any

from httpx import AsyncClient

from app.dependencies import get_sessionmaker
from app.repositories.interview_repository import InterviewRepository
from app.repositories.job_repository import JobRepository
from app.repositories.question_repository import QuestionRepository
from app.repositories.resume_repository import ResumeRepository
from app.schemas.interview import InterviewHeadFields, InterviewListItem, InterviewOut
from app.schemas.job import JD_EXCERPT_CHARS, JobFields, JobListItem, JobOut
from app.schemas.resume import SKILL_PREVIEW_LIMIT, ResumeFields, ResumeListItem, ResumeOut
from app.utils.db import session_scope

_RESUMES = "/api/v1/resumes/list"
_JOBS = "/api/v1/jobs"
_INTERVIEWS = "/api/v1/interviews/list"

#: 一份"够肥"的抽取结果：列表若漏掉了投影，这一行就能把载荷撑起来（用例 4 靠它才有落差）。
_FAT_SKILLS: list[dict[str, Any]] = [
    {
        "name": f"技能{i}",
        "level": "proficient",
        "evidence": "证据片段" * 20,
    }
    for i in range(30)
]
_FAT_PARSED: dict[str, Any] = {
    "name": "林可",
    "years_experience": 7.5,
    "skills": _FAT_SKILLS,
    "educations": [
        {"school": f"某校{i}", "degree": "bachelor", "evidence": "证据" * 20} for i in range(3)
    ],
    "work_experiences": [
        {"company": f"某厂{i}", "evidence": "证据片段" * 20} for i in range(6)
    ],
    "projects": [{"name": f"项目{i}", "evidence": "证据片段" * 20} for i in range(8)],
}
_LONG_JD = "岗位职责：负责分布式服务的开发与优化。" * 60
_QUESTIONS: list[dict[str, Any]] = [
    {
        "stem": "讲讲令牌桶与漏桶的区别",
        "difficulty": "medium",
        "source": "resume",
        "reference_answer": "先说结构再说选型",
        "checkpoints": ["说出两者限流突发能力的差别"],
        "rubric": [],
        "follow_ups": [],
        "evidence": "简历：网关限流",
    }
]


async def _seed_resume(parsed: dict[str, Any] | None = None) -> str:
    async with session_scope(get_sessionmaker()) as session:
        row = await ResumeRepository(session).create(
            name="林可",
            file_name="林可.pdf",
            raw_text="林可的简历原文",
            parsed=parsed if parsed is not None else _FAT_PARSED,
        )
        return row.id


async def _seed_job(jd_text: str = _LONG_JD) -> str:
    async with session_scope(get_sessionmaker()) as session:
        row = await JobRepository(session).create(
            title="后端工程师", company="示例公司", jd_text=jd_text, required_skills=["python"]
        )
        return row.id


async def _seed_report() -> str:
    """一份 completed 报告 + 一行题目：列表该没有 `questions`，详情该有。"""
    async with session_scope(get_sessionmaker()) as session:
        resume = await ResumeRepository(session).create(
            name="林可", file_name="林可.pdf", raw_text="林可的简历原文"
        )
        head = await InterviewRepository(session).create(
            resume_id=resume.id,
            job_id=None,
            status="completed",
            plan={"total": 1, "strategy": "weak_first"},
            review_issues=[{"index": 0, "problem": "证据不在原文里", "origin": "rule"}],
            total_questions=len(_QUESTIONS),
            resume_name="林可",
            job_title="后端工程师",
        )
        await QuestionRepository(session).add_many(_QUESTIONS, interview_id=head.id, batch=1)
        return head.id


# --- 简历 ---


async def test_resume_list_row_has_no_parsed_but_carries_an_overview(
    client: AsyncClient,
) -> None:
    await _seed_resume()

    row = (await client.get(_RESUMES)).json()["items"][0]

    assert "parsed" not in row
    assert row["overview"]["skill_count"] == len(_FAT_PARSED["skills"])
    assert row["overview"]["years_experience"] == _FAT_PARSED["years_experience"]
    assert row["overview"]["work_experience_count"] == len(_FAT_PARSED["work_experiences"])
    # 概览只带前 N 个技能名：列表页与出题下拉都只渲染前 8 个。
    assert row["overview"]["skill_names"] == [s["name"] for s in _FAT_SKILLS[:SKILL_PREVIEW_LIMIT]]


async def test_resume_overview_survives_a_thin_or_broken_parsed(
    client: AsyncClient,
) -> None:
    """概览是展示用的：历史行少键、键型不对都不该把整张列表拖成 500。

    `parsed` 是"当年通过校验的那份 dump"（`ResumeOut` 的注释），用今天的 schema 回读它
    本来就会误判，所以概览也只能逐项容错——这一条钉的就是那个容错。
    """
    await _seed_resume({"name": "林可"})

    row = (await client.get(_RESUMES)).json()["items"][0]

    assert row["overview"] == {
        "skill_count": 0,
        "skill_names": [],
        "years_experience": None,
        "work_experience_count": 0,
    }


async def test_resume_detail_still_returns_the_full_parsed(client: AsyncClient) -> None:
    resume_id = await _seed_resume()

    body = (await client.get(f"/api/v1/resumes/{resume_id}")).json()

    assert body["parsed"] == _FAT_PARSED
    # 详情与列表共用列：`parsed` 之外的那几列必须是同一批值。
    assert body["name"] == "林可"


# --- 岗位 ---


async def test_job_list_row_carries_the_excerpt_not_the_whole_jd(client: AsyncClient) -> None:
    await _seed_job()

    row = (await client.get(_JOBS)).json()["items"][0]

    assert "jd_text" not in row
    assert row["jd_excerpt"] == _LONG_JD[:JD_EXCERPT_CHARS]


async def test_a_short_jd_is_not_cut_by_the_excerpt(client: AsyncClient) -> None:
    """摘要只是"截到那么多"，不是"凑到那么多"：短 JD 该整条给，别为它补省略号。"""
    await _seed_job(jd_text="精通 Python")

    row = (await client.get(_JOBS)).json()["items"][0]

    assert row["jd_excerpt"] == "精通 Python"


async def test_job_detail_still_returns_the_full_jd(client: AsyncClient) -> None:
    job_id = await _seed_job()

    body = (await client.get(f"/api/v1/jobs/{job_id}")).json()

    assert body["jd_text"] == _LONG_JD
    assert body["required_skills"] == ["python"]


# --- 报告 ---


async def test_interview_list_row_has_no_questions_key(client: AsyncClient) -> None:
    """不是"空数组"，是**没有这个键**。

    空数组同时是"这份卷子确实没题"（`aborted` 的真实状态）和"列表不下发正文"，
    读的人分不清——`ReportsView` 导出前必须先取一次详情就是为它打的补丁。
    """
    await _seed_report()

    row = (await client.get(_INTERVIEWS)).json()["items"][0]

    assert "questions" not in row
    # 批注与计划留在列表行里：报告中心的展开行直接渲染它们，不逐行请求详情。
    assert row["review_issues"][0]["problem"] == "证据不在原文里"
    assert row["plan"]["strategy"] == "weak_first"
    assert row["total_questions"] == 1


async def test_interview_detail_still_returns_the_questions(client: AsyncClient) -> None:
    interview_id = await _seed_report()

    body = (await client.get(f"/api/v1/interviews/{interview_id}")).json()

    assert [q["stem"] for q in body["questions"]] == [_QUESTIONS[0]["stem"]]
    assert "batch" not in body["questions"][0]  # 工序列不外泄（M6 就钉住了）


# --- 收益：列表载荷确实比详情小 ---


async def test_the_list_payload_is_smaller_than_the_detail_one(client: AsyncClient) -> None:
    """投影的价值是字节数，那就用字节数验收（卡片承诺"几百 KB → 几 KB"）。

    只断言"小得多"而不断言具体大小：具体值随 `parsed` 内容漂，钉住它等于把 fixture 的形状
    写进契约里。
    """
    resume_id = await _seed_resume()
    job_id = await _seed_job()

    resumes_bytes = len(json.dumps((await client.get(_RESUMES)).json(), ensure_ascii=False))
    detail_bytes = len(
        json.dumps((await client.get(f"/api/v1/resumes/{resume_id}")).json(), ensure_ascii=False)
    )
    jobs_bytes = len(json.dumps((await client.get(_JOBS)).json(), ensure_ascii=False))
    job_detail_bytes = len(
        json.dumps((await client.get(f"/api/v1/jobs/{job_id}")).json(), ensure_ascii=False)
    )

    assert resumes_bytes * 5 < detail_bytes
    assert jobs_bytes < job_detail_bytes


# --- 结构护栏：列表与详情共用同一个核心模型 ---


_SHARES = [
    (ResumeFields, ResumeListItem, ResumeOut, "parsed"),
    (JobFields, JobListItem, JobOut, "jd_text"),
    (InterviewHeadFields, InterviewListItem, InterviewOut, "questions"),
]


def test_list_and_detail_models_share_one_core() -> None:
    """`_SHARES` 的三对模型都必须继承同一个核心，且正文列只在详情那一侧。

    这是卡片那句"别拆两份 DTO 真相"唯一能机械化检查的形状：两份平行模型的字段
    哪怕此刻完全一致，也不会在任何人只改一处时报红；继承关系断了才说明有人抄了一份。
    """
    for core, list_model, detail_model, body_column in _SHARES:
        assert issubclass(list_model, core), list_model
        assert issubclass(detail_model, core), detail_model
        assert set(core.model_fields) <= set(list_model.model_fields)
        assert set(core.model_fields) <= set(detail_model.model_fields)
        assert body_column not in list_model.model_fields
        assert body_column in detail_model.model_fields
