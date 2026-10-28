"""岗位 / 简历的写端点测试：CRUD 的接缝 + 报告列表的"哪份简历 × 哪个岗位"。

夹具来自 `tests/test_api/conftest.py`（真 `create_app()` + 单例引擎 + `tmp_path` 里的独立库
与上传目录），本文件只额外加一个 autouse fixture 把两个后台执行体换成空协程：这里测的是
写路径的后果与事务边界，不跑任务本体。

三条只在这里被测得到的事：

1. **删除的传播方向**是数据库定的（`interview.job_id` SET NULL、`interview.resume_id`
   CASCADE），只有真跑一遍 DELETE 才知道报告头里的快照列顶不顶得住；
2. **409 是判出来的，不是撞出来的**：`uq_job_title_company` 约束在 SQLite 里照样会拦，
   但那时响应是 500；先按自然键查才换得来可分支的错误体；
3. **简历删除默认不放行**：级联会带走报告与题目，而这个仓库没有第二份数据。

断言一律落在状态码、`error.code`、行数与字段值上，不比中文文案。
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import interviews as interviews_api
from app.api.v1 import resumes as resumes_api
from app.config import get_settings
from app.dependencies import get_sessionmaker
from app.models.interview import Interview
from app.models.job import Job
from app.models.question import Question
from app.models.resume import Resume
from app.repositories.interview_repository import InterviewRepository
from app.repositories.question_repository import QuestionRepository
from app.repositories.resume_repository import ResumeRepository
from app.utils.db import session_scope

from ._stubs import PDF_BYTES

JD_TEXT = "负责高并发 API 网关；精通 Python，熟悉 Redis；要求掌握 MySQL 索引优化"


@pytest.fixture(autouse=True)
def _background_tasks_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """两个后台执行体一律换成空协程：本文件测写路径的后果与事务边界，不跑任务本体
    （执行体在 `tests/test_tasks/`，接缝在 `test_m6_api.py`）。
    """

    async def swallow(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(interviews_api, "run_question_generation", swallow)
    monkeypatch.setattr(resumes_api, "run_resume_parsing", swallow)


async def _count(session: AsyncSession, model: type) -> int:
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def _seed_resume(name: str = "林可") -> str:
    async with session_scope(get_sessionmaker()) as session:
        row = await ResumeRepository(session).create(
            name=name, raw_text=f"{name} 的简历原文", file_name=f"{name}.pdf"
        )
        return row.id


# --- POST /jobs（JSON）---


async def test_create_job_derives_the_dictionary_skills(client: AsyncClient) -> None:
    """不给技能清单时由词典派生——与 `match` 同一本词典，两侧不会分叉。"""
    response = await client.post(
        "/api/v1/jobs", json={"title": "后端工程师", "company": "示例公司", "jd_text": JD_TEXT}
    )

    assert response.status_code == 201
    body = response.json()
    skills = body["required_skills"]
    assert "python" in skills and "redis" in skills and "mysql" in skills
    assert all(name == name.lower() for name in skills)
    assert body["jd_text"] == JD_TEXT

    # 写入必须真的提交：端点返回后另开事务读得到
    async with session_scope(get_sessionmaker()) as session:
        row = await session.get(Job, body["id"])
        assert row is not None and row.title == "后端工程师"


async def test_explicit_skill_list_wins_over_derivation(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/jobs",
        json={
            "title": "数据工程师",
            "company": "示例公司",
            "jd_text": JD_TEXT,
            "required_skills": ["Kafka", " kafka ", "Flink"],
        },
    )

    assert response.status_code == 201
    # 以人给的为准：只做小写归一与去重，不混进派生的三个
    assert response.json()["required_skills"] == ["kafka", "flink"]


async def test_duplicate_natural_key_is_409_and_stays_single_row(client: AsyncClient) -> None:
    payload = {"title": "后端工程师", "company": "示例公司", "jd_text": JD_TEXT}
    first = await client.post("/api/v1/jobs", json=payload)

    second = await client.post("/api/v1/jobs", json=payload)

    assert second.status_code == 409
    error = second.json()["error"]
    assert error["code"] == "already_exists"
    assert error["details"]["job_id"] == first.json()["id"]
    async with session_scope(get_sessionmaker()) as session:
        assert await _count(session, Job) == 1


@pytest.mark.parametrize("field", ["title", "jd_text"])
async def test_create_job_rejects_a_missing_required_field(client: AsyncClient, field: str) -> None:
    payload = {"title": "后端工程师", "jd_text": JD_TEXT}
    payload.pop(field)
    response = await client.post("/api/v1/jobs", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"
    async with session_scope(get_sessionmaker()) as session:
        assert await _count(session, Job) == 0


async def test_create_job_trims_the_natural_key(client: AsyncClient) -> None:
    """`"后端工程师   "` 与 `"后端工程师"` 必须是同一个岗位，否则去重形同虚设。"""
    first = await client.post(
        "/api/v1/jobs", json={"title": "后端工程师", "company": "示例公司", "jd_text": JD_TEXT}
    )

    padded = await client.post(
        "/api/v1/jobs", json={"title": "  后端工程师 ", "company": "示例公司", "jd_text": JD_TEXT}
    )

    assert first.json()["title"] == "后端工程师"
    assert padded.status_code == 409

    blank = await client.post("/api/v1/jobs", json={"title": "   ", "jd_text": JD_TEXT})
    assert blank.status_code == 422


# --- POST /jobs/upload（文件）---


async def test_upload_job_stores_the_file_text_as_jd_text(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/jobs/upload",
        files={"file": ("岗位说明.txt", JD_TEXT.encode("utf-8"), "text/plain")},
        data={"title": "平台工程师", "company": "示例公司"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "平台工程师"
    assert body["jd_text"] == JD_TEXT
    assert "python" in body["required_skills"]


async def test_upload_job_decodes_a_gbk_text_file(client: AsyncClient) -> None:
    """记事本另存的 .txt 默认是 GBK。只按 UTF-8 解的话，中文 JD 会当场 500。"""
    response = await client.post(
        "/api/v1/jobs/upload",
        files={"file": ("岗位说明.txt", JD_TEXT.encode("gb18030"), "text/plain")},
        data={"title": "平台工程师"},
    )

    assert response.status_code == 201
    assert response.json()["jd_text"] == JD_TEXT


@pytest.mark.parametrize(
    ("file_name", "payload", "code"),
    [
        ("jd.exe", JD_TEXT.encode("utf-8"), "jd_bad_suffix"),
        ("jd.txt", b"", "jd_empty_file"),
        ("jd.txt", "只要三人".encode(), "jd_too_short"),
    ],
)
async def test_upload_job_refusals_leave_no_row(
    client: AsyncClient, file_name: str, payload: bytes, code: str
) -> None:
    response = await client.post(
        "/api/v1/jobs/upload",
        files={"file": (file_name, payload, "application/octet-stream")},
        data={"title": "平台工程师"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == code
    async with session_scope(get_sessionmaker()) as session:
        assert await _count(session, Job) == 0


async def test_upload_job_without_title_is_422(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/jobs/upload",
        files={"file": ("jd.txt", JD_TEXT.encode("utf-8"), "text/plain")},
        data={"title": "   "},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "jd_missing_title"


# --- GET /jobs/{id} · PUT · DELETE ---


async def test_job_read_back_and_404_envelope(client: AsyncClient) -> None:
    created = await client.post(
        "/api/v1/jobs", json={"title": "后端工程师", "jd_text": JD_TEXT}
    )
    job_id = created.json()["id"]

    body = (await client.get(f"/api/v1/jobs/{job_id}")).json()
    assert body["id"] == job_id
    assert body["min_years"] == 0.0

    missing = await client.get("/api/v1/jobs/no-such-job")
    assert missing.status_code == 404
    assert missing.json()["error"]["details"] == {"job_id": "no-such-job"}


async def test_put_job_re_derives_skills_when_the_jd_changes(client: AsyncClient) -> None:
    created = await client.post(
        "/api/v1/jobs", json={"title": "后端工程师", "jd_text": JD_TEXT}
    )
    job_id = created.json()["id"]

    response = await client.put(
        f"/api/v1/jobs/{job_id}", json={"jd_text": "负责消息队列，精通 Kafka"}
    )

    assert response.status_code == 200
    assert response.json()["jd_text"] == "负责消息队列，精通 Kafka"
    # 旧清单留着等于让 match 拿过期的要求比新 JD
    assert "python" not in response.json()["required_skills"]
    assert "kafka" in response.json()["required_skills"]

    renamed = await client.put(f"/api/v1/jobs/{job_id}", json={"company": "另一家公司"})
    assert renamed.status_code == 200
    assert renamed.json()["company"] == "另一家公司"


async def test_put_job_needs_at_least_one_field_and_an_existing_row(client: AsyncClient) -> None:
    created = await client.post(
        "/api/v1/jobs", json={"title": "后端工程师", "jd_text": JD_TEXT}
    )
    empty = await client.put(f"/api/v1/jobs/{created.json()['id']}", json={})
    assert empty.status_code == 422

    missing = await client.put("/api/v1/jobs/no-such-job", json={"company": "甲"})
    assert missing.status_code == 404


async def test_put_job_cannot_collide_with_another_rows_natural_key(client: AsyncClient) -> None:
    await client.post("/api/v1/jobs", json={"title": "甲", "company": "同厂", "jd_text": JD_TEXT})
    other = await client.post(
        "/api/v1/jobs", json={"title": "乙", "company": "另一厂", "jd_text": JD_TEXT}
    )

    clash = await client.put(
        f"/api/v1/jobs/{other.json()['id']}", json={"title": "甲", "company": "同厂"}
    )

    assert clash.status_code == 409
    async with session_scope(get_sessionmaker()) as session:
        assert await _count(session, Job) == 2


async def test_delete_job_keeps_the_report_and_its_snapshot(client: AsyncClient) -> None:
    """岗位可下线：`interview.job_id` 是 SET NULL，报告靠标题/公司快照列自证出身。"""
    resume_id = await _seed_resume()
    job_id = await _create_job(client)
    interview_id = await _start_report(client, resume_id, job_id)

    response = await client.delete(f"/api/v1/jobs/{job_id}")

    assert response.status_code == 204
    async with session_scope(get_sessionmaker()) as session:
        assert await _count(session, Job) == 0
        head = await session.get(Interview, interview_id)
        assert head is not None and head.job_id is None
        assert head.job_title == "后端工程师" and head.job_company == "示例公司"

    body = (await client.get(f"/api/v1/interviews/{interview_id}")).json()
    assert body["job_id"] is None
    assert body["job_title"] == "后端工程师"

    missing = await client.delete(f"/api/v1/jobs/{job_id}")
    assert missing.status_code == 404


# --- PUT /resumes/{id} · DELETE /resumes/{id} ---


async def test_rename_resume_persists(client: AsyncClient) -> None:
    resume_id = await _seed_resume("林可")

    response = await client.put(f"/api/v1/resumes/{resume_id}", json={"name": "  林可（更正）  "})

    assert response.status_code == 200
    # 首尾空白在入库前剥掉：展示名带尾空格会一路带进报告快照列
    assert response.json()["name"] == "林可（更正）"
    async with session_scope(get_sessionmaker()) as session:
        row = await session.get(Resume, resume_id)
        assert row is not None and row.name == "林可（更正）"


@pytest.mark.parametrize("name", ["", "   "])
async def test_rename_resume_rejects_blank_and_unknown(client: AsyncClient, name: str) -> None:
    resume_id = await _seed_resume()

    response = await client.put(f"/api/v1/resumes/{resume_id}", json={"name": name})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"

    missing = await client.put("/api/v1/resumes/no-such-resume", json={"name": "甲"})
    assert missing.status_code == 404


async def test_delete_resume_is_refused_while_reports_exist(client: AsyncClient) -> None:
    """级联会带走报告与题目，所以默认拒；拒了就必须什么都没动。"""
    resume_id = await _seed_resume()
    job_id = await _create_job(client)
    interview_id = await _start_report(client, resume_id, job_id)

    response = await client.delete(f"/api/v1/resumes/{resume_id}")

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "already_exists"
    assert error["details"] == {"resume_id": resume_id, "interview_count": 1}
    async with session_scope(get_sessionmaker()) as session:
        assert await _count(session, Resume) == 1
        assert await InterviewRepository(session).list_for_resume(resume_id)
        assert (await session.get(Interview, interview_id)) is not None


async def test_force_delete_removes_reports_and_the_stored_file(client: AsyncClient) -> None:
    uploaded = await client.post(
        "/api/v1/resumes/upload", files={"file": ("林可.pdf", PDF_BYTES, "application/pdf")}
    )
    resume_id = uploaded.json()["resume_id"]
    stored = get_settings().upload_dir / f"{resume_id}.pdf"
    assert stored.is_file()

    job_id = await _create_job(client)
    interview_id = await _start_report(client, resume_id, job_id)
    async with session_scope(get_sessionmaker()) as session:
        await QuestionRepository(session).add_many(
            [{"stem": "讲讲 Redis 的持久化", "difficulty": "medium", "source": "resume"}],
            interview_id=interview_id,
            batch=1,
        )

    response = await client.delete(f"/api/v1/resumes/{resume_id}", params={"force": "true"})

    assert response.status_code == 204
    async with session_scope(get_sessionmaker()) as session:
        # `interview.resume_id` 是 ON DELETE CASCADE：报告与题目跟着简历走
        assert await _count(session, Resume) == 0
        assert await _count(session, Interview) == 0
        assert await _count(session, Question) == 0
    assert not stored.exists()
    assert (await client.get(f"/api/v1/interviews/{interview_id}")).status_code == 404


async def test_delete_resume_without_reports_removes_the_file(client: AsyncClient) -> None:
    uploaded = await client.post(
        "/api/v1/resumes/upload", files={"file": ("甲.pdf", PDF_BYTES, "application/pdf")}
    )
    resume_id = uploaded.json()["resume_id"]
    stored = get_settings().upload_dir / f"{resume_id}.pdf"

    response = await client.delete(f"/api/v1/resumes/{resume_id}")

    assert response.status_code == 204
    assert not stored.exists()
    async with session_scope(get_sessionmaker()) as session:
        assert await _count(session, Resume) == 0

    missing = await client.delete(f"/api/v1/resumes/{resume_id}")
    assert missing.status_code == 404


# --- 报告列表：哪份简历 × 哪个岗位 ---


async def test_report_list_names_the_resume_and_the_job_it_was_generated_against(
    client: AsyncClient,
) -> None:
    """列表页不许靠回 join 认出身：快照列在生成那一刻就写进 `interview` 行。"""
    resume_id = await _seed_resume("林可")
    job_id = await _create_job(client)
    interview_id = await _start_report(client, resume_id, job_id)

    body = (await client.get("/api/v1/interviews/list")).json()["items"]

    assert len(body) == 1
    head = body[0]
    assert head["id"] == interview_id
    assert head["resume_name"] == "林可"
    assert head["resume_file_name"] == "林可.pdf"
    assert head["job_title"] == "后端工程师"
    assert head["job_company"] == "示例公司"


async def test_report_snapshot_survives_a_resume_rename(client: AsyncClient) -> None:
    """改名只改 `resume.name`，报告里的快照是"当时叫什么"，不跟着漂移。"""
    resume_id = await _seed_resume("林可")
    job_id = await _create_job(client)
    interview_id = await _start_report(client, resume_id, job_id)

    await client.put(f"/api/v1/resumes/{resume_id}", json={"name": "林小可"})

    head = (await client.get(f"/api/v1/interviews/{interview_id}")).json()
    assert head["resume_name"] == "林可"


# --- 共用夹具动作 ---


async def _create_job(client: AsyncClient, title: str = "后端工程师") -> str:
    response = await client.post(
        "/api/v1/jobs",
        json={"title": title, "company": "示例公司", "jd_text": JD_TEXT},
    )
    assert response.status_code == 201
    return str(response.json()["id"])


async def _start_report(client: AsyncClient, resume_id: str, job_id: str) -> str:
    """起一份报告：后台执行体已被 autouse 替身吞掉，这里只留下报告头与快照列。"""
    response = await client.post(
        "/api/v1/interviews/generate", json={"resume_id": resume_id, "job_id": job_id}
    )
    assert response.status_code == 202
    return str(response.json()["interview_id"])
