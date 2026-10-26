"""M6 后台化的端点级测试：真 app + 真 SQLite + **假后台任务**。

这里要钉住的是"API 与后台的接缝"，不是后台本身（`tests/test_tasks/` 测执行体）：

1. 202 回执带两个 id，且**任务函数拿到的参数**与库里那两行对得上；
2. 后台协程跑起来时业务行**已经提交**——这条在 M6 是真踩出来的：实测（FastAPI 0.143 +
   Starlette 1.8）后台在 `get_db` 收尾**之前**就执行，所以"排后台任务"的端点不能靠请求
   会话提交。验证方式只能是让替身**另开一个会话**去读：同一个会话里读得到只证明缓存，
   不证明提交；
3. 失败路径拒在落盘与建行之前，脏文件名和不完成的行都不许留下。

异常体形状（`{"error": {code, message, details}}`）也是被测对象：它是客户端唯一能据以
分支的契约。断言只落在 code/结构上，从不落中文文案。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import interviews as interviews_api
from app.api.v1 import jobs as jobs_api
from app.api.v1 import resumes as resumes_api
from app.config import get_settings
from app.dependencies import dispose_engine, get_engine, get_sessionmaker
from app.main import create_app
from app.models import Base
from app.models.interview import Interview
from app.models.resume import Resume
from app.models.task import KIND_QUESTION_GENERATION, KIND_RESUME_PARSING, Task
from app.repositories.interview_repository import InterviewRepository
from app.repositories.job_repository import JobRepository
from app.repositories.question_repository import QuestionRepository
from app.repositories.resume_repository import ResumeRepository
from app.services import task_service
from app.utils.db import session_scope

#: 只走落盘、不真解析的最小伪 PDF（bytes 字面量必须纯 ASCII）
PDF_BYTES = b"%PDF-1.4\n% stub body for upload staging only\n%%EOF\n"


@pytest.fixture(autouse=True)
def _settings_point_at_this_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """把两份"进程级真相"（数据库 URL、上传目录）都指到本用例的 `tmp_path`。

    上传目录不隔离就会写进 `data/uploads/`——测试顺手往开发目录灌垃圾是迟早的事。
    """
    settings = get_settings()
    url = f"sqlite+aiosqlite:///{(tmp_path / 'api.db').as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    """真 `create_app()` + 建表：路由、依赖、异常处理器全按生产装配。

    建表走 `Base.metadata.create_all`，但**引擎用单例**——另起一台就是第二个连接池
    抢同一个 SQLite 文件，正是 AGENTS.md §3 禁止的形状。
    """
    await dispose_engine()
    engine = get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield create_app()
    await dispose_engine()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://if.test") as new:
        yield new


async def _count(session: AsyncSession, model: type) -> int:
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def _seed_resume(name: str = "林可") -> str:
    async with session_scope(get_sessionmaker()) as session:
        row = await ResumeRepository(session).create(
            name=name, raw_text=f"{name} 的简历原文", file_name=f"{name}.pdf"
        )
        return row.id


async def _seed_job(title: str = "后端工程师", jd_text: str = "精通 Python 与 PostgreSQL") -> str:
    async with session_scope(get_sessionmaker()) as session:
        row = await JobRepository(session).create(
            title=title, company="示例公司", jd_text=jd_text, required_skills=["python"]
        )
        return row.id


# --- GET /health ---


async def test_health_names_the_running_environment(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["env"] == get_settings().app_env


# --- POST /resumes/upload ---


async def test_upload_returns_ids_and_the_background_finds_them_committed(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验收前半段：立刻返回 202 + 两个 id，且后台任务读得到**已提交**的简历行。"""
    seen: list[dict[str, Any]] = []

    async def fake_task(task_id: str, resume_id: str) -> None:
        async with session_scope(get_sessionmaker()) as session:
            row = await session.get(Resume, resume_id)
            task = await session.get(Task, task_id)
        seen.append(
            {"resume_exists": row is not None, "task_status": task.status if task else None}
        )

    monkeypatch.setattr(resumes_api, "run_resume_parsing", fake_task)

    response = await client.post(
        "/api/v1/resumes/upload", files={"file": ("梁清生.pdf", PDF_BYTES, "application/pdf")}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    # 替身是在响应返回前被 Starlette 跑起来的，此刻它已经读得到两行 → 提交确实发生在排任务之前
    assert seen == [{"resume_exists": True, "task_status": "pending"}]
    assert body["resume_id"] != body["task_id"]

    # 落盘名固定 `{resume_id}{suffix}`：用户文件名不参与路径构造，穿越与覆盖在设计上关门
    staged = get_settings().upload_dir / f"{body['resume_id']}.pdf"
    assert staged.read_bytes() == PDF_BYTES

    async with session_scope(get_sessionmaker()) as session:
        task = await session.get(Task, body["task_id"])
        assert task is not None
        assert task.kind == KIND_RESUME_PARSING
        assert task.ref_id == body["resume_id"]


@pytest.mark.parametrize(
    ("file_name", "payload", "code"),
    [
        ("resume.exe", PDF_BYTES, "upload_bad_suffix"),
        ("resume.pdf", b"", "upload_empty_file"),
    ],
)
async def test_upload_refusals_leave_nothing_behind(
    client: AsyncClient, file_name: str, payload: bytes, code: str
) -> None:
    response = await client.post(
        "/api/v1/resumes/upload", files={"file": (file_name, payload, "application/pdf")}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == code

    upload_dir = get_settings().upload_dir
    assert not upload_dir.exists() or list(upload_dir.iterdir()) == []
    async with session_scope(get_sessionmaker()) as session:
        assert await _count(session, Resume) == 0
        assert await _count(session, Task) == 0


# --- GET /resumes ---


async def test_resume_read_back_returns_the_stored_json(client: AsyncClient) -> None:
    resume_id = await _seed_resume()
    async with session_scope(get_sessionmaker()) as session:
        await ResumeRepository(session).update(resume_id, parsed={"name": "林可"})

    response = await client.get(f"/api/v1/resumes/{resume_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == resume_id
    assert body["name"] == "林可"
    assert body["parsed"] == {"name": "林可"}
    # `stored_path` 是工序列，不进 DTO
    assert "stored_path" not in body


async def test_unknown_resume_is_404_with_the_shared_envelope(client: AsyncClient) -> None:
    response = await client.get("/api/v1/resumes/no-such-id")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["details"] == {"resume_id": "no-such-id"}


async def test_list_resumes_is_registered_before_the_id_route(client: AsyncClient) -> None:
    """/list 若排在 `/{resume_id}` 之后，路径段 "list" 会被当成 id 直接 404。"""
    await _seed_resume("甲")
    await _seed_resume("乙")

    full = await client.get("/api/v1/resumes/list")
    assert full.status_code == 200
    assert len(full.json()) == 2

    paged = await client.get("/api/v1/resumes/list", params={"limit": 1})
    assert len(paged.json()) == 1


# --- GET /tasks/{task_id} ---


async def test_task_read_back_and_404(client: AsyncClient) -> None:
    async with session_scope(get_sessionmaker()) as session:
        task = await task_service.create_task(
            session, kind=KIND_QUESTION_GENERATION, ref_id="iv-1"
        )
        task_id = task.id

    body = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    assert body["kind"] == KIND_QUESTION_GENERATION
    assert body["status"] == "pending"
    assert body["ref_id"] == "iv-1"
    assert body["result"] == {}

    missing = await client.get("/api/v1/tasks/no-such-task")
    assert missing.status_code == 404
    assert missing.json()["error"]["details"] == {"task_id": "no-such-task"}


async def test_polling_sees_progress_written_by_the_background(client: AsyncClient) -> None:
    """轮询的全部意义：后台每跑完一个节点，进度就**已经躺在库里**。"""
    resume_id = await _seed_resume()
    async with session_scope(get_sessionmaker()) as session:
        task = await task_service.create_task(session, kind=KIND_RESUME_PARSING, ref_id=resume_id)
        task_id = task.id

    maker = get_sessionmaker()
    await task_service.mark_running(maker, task_id)
    await task_service.record_step(maker, task_id, "parse", ms=12.5)

    body = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    assert body["status"] == "running"
    assert body["progress"]["steps"] == [{"node": "parse", "ms": 12.5}]


# --- POST /interviews/generate ---


async def test_generate_creates_a_running_head_and_hands_jd_text_to_the_task(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, str, str]] = []

    async def fake_task(task_id: str, resume_id: str, interview_id: str, jd_text: str) -> None:
        calls.append((task_id, resume_id, interview_id, jd_text))

    monkeypatch.setattr(interviews_api, "run_question_generation", fake_task)

    resume_id = await _seed_resume()
    job_id = await _seed_job(jd_text="负责高并发的 API 网关")

    response = await client.post(
        "/api/v1/interviews/generate", json={"resume_id": resume_id, "job_id": job_id}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "running"
    assert calls == [(body["task_id"], resume_id, body["interview_id"], "负责高并发的 API 网关")]

    async with session_scope(get_sessionmaker()) as session:
        head = await session.get(Interview, body["interview_id"])
        task = await session.get(Task, body["task_id"])
        assert head is not None and head.status == "running"
        assert head.resume_id == resume_id and head.job_id == job_id
        assert task is not None and task.kind == KIND_QUESTION_GENERATION
        assert task.ref_id == body["interview_id"]


@pytest.mark.parametrize("field", ["resume_id", "job_id"])
async def test_generate_rejects_an_unknown_reference(
    client: AsyncClient, field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """404 判在服务层，端点只做翻译；且不能留下报告头或任务行、不能排上后台。"""
    calls: list[Any] = []

    async def fake_task(*args: Any) -> None:
        calls.append(args)

    monkeypatch.setattr(interviews_api, "run_question_generation", fake_task)

    payload = {"resume_id": await _seed_resume(), "job_id": await _seed_job()}
    payload[field] = "no-such-row"

    response = await client.post("/api/v1/interviews/generate", json=payload)

    assert response.status_code == 404
    assert field in response.json()["error"]["details"]
    assert calls == []
    async with session_scope(get_sessionmaker()) as session:
        assert await _count(session, Interview) == 0
        assert await _count(session, Task) == 0


async def test_generate_body_error_uses_the_shared_envelope_without_echo(
    client: AsyncClient,
) -> None:
    """422 只回 `loc` 与 `msg`：FastAPI 默认体把输入原样塞进 `ctx`，简历字段就是外泄面。"""
    response = await client.post("/api/v1/interviews/generate", json={"resume_id": ""})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_failed"
    problems = error["details"]["problems"]
    assert {problem["loc"][-1] for problem in problems} == {"resume_id", "job_id"}
    assert all("ctx" not in problem for problem in problems)


# --- GET /interviews/{id} ---


async def _seed_report(resume_id: str, job_id: str, *, status: str) -> str:
    """建报告头；`completed` 时写两个批次各两题（旧批次留在表里，读报告只取最新那一批）。"""
    async with session_scope(get_sessionmaker()) as session:
        head = await InterviewRepository(session).create(
            resume_id=resume_id, job_id=job_id, plan={}, status=status
        )
        if status == "completed":
            batches = {
                1: ["第一批的旧题A", "第一批的旧题B"],
                2: ["第二批第一题", "第二批第二题"],
            }
            for batch, stems in batches.items():
                await QuestionRepository(session).add_many(
                    [
                        {
                            "stem": stem,
                            "difficulty": "easy",
                            "source": "generic",
                            # 读报告要重新过 `QuestionSchema`，考察点/参考答案是必填项
                            "checkpoints": ["能讲清取舍"],
                            "reference_answer": "参考答案要点",
                        }
                        for stem in stems
                    ],
                    interview_id=head.id,
                    batch=batch,
                )
        return head.id


async def test_report_read_returns_only_the_latest_batch_in_order(client: AsyncClient) -> None:
    resume_id = await _seed_resume()
    interview_id = await _seed_report(resume_id, await _seed_job(), status="completed")

    body = (await client.get(f"/api/v1/interviews/{interview_id}")).json()

    assert body["status"] == "completed"
    assert [q["stem"] for q in body["questions"]] == ["第二批第一题", "第二批第二题"]
    assert [q["order_index"] for q in body["questions"]] == [0, 1]
    assert "batch" not in body["questions"][0]  # 工序列不外泄


async def test_report_read_is_empty_while_running(client: AsyncClient) -> None:
    """跑中途轮询报告：合法响应是 `running` + 空题目，不是 404、也不是半份报告。"""
    interview_id = await _seed_report(await _seed_resume(), await _seed_job(), status="running")

    body = (await client.get(f"/api/v1/interviews/{interview_id}")).json()
    assert body["status"] == "running"
    assert body["questions"] == []


async def test_unknown_interview_is_404(client: AsyncClient) -> None:
    response = await client.get("/api/v1/interviews/nope")
    assert response.status_code == 404
    assert response.json()["error"]["details"] == {"interview_id": "nope"}


# --- GET /jobs ---


async def test_jobs_list_exposes_the_choice_generate_needs(client: AsyncClient) -> None:
    job_id = await _seed_job(title="数据平台工程师")
    body = (await client.get("/api/v1/jobs")).json()
    assert [job["id"] for job in body] == [job_id]
    assert body[0]["title"] == "数据平台工程师"


# --- 未预期异常：500 外壳 ---


async def test_an_unexpected_error_does_not_leak_the_stack(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """500 外壳只给固定文案。

    `raise_app_exceptions=False` 是必须的：Starlette 的 `ServerErrorMiddleware` 在调用完
    自定义处理器后会把异常**重新抛出**（好让日志有堆栈），而 httpx 的 ASGI 传输默认会把
    它再抛给测试。这里要验的是"客户端拿到的响应体"，堆栈那一份本来就不该出门。
    """

    async def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("内部细节：/secret/path 上的第 42 行")

    monkeypatch.setattr(jobs_api.job_service, "list_jobs", explode)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://if.test") as client:
        response = await client.get("/api/v1/jobs")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "secret" not in response.text
    assert "RuntimeError" not in response.text


# --- 控制台静态页 ---


@pytest.fixture
def console_dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时构建产物：只造 `index.html` 和一个带扩展名的资源。

    不读真 `frontend/dist`：CI 不跑 npm，页面文案也会独立演进——这里钉的是**挂载契约**，
    不是产物内容。
    """
    from app import main as main_module

    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text(
        '<!doctype html><html lang="zh-CN"><body><div id="app"></div></body></html>',
        encoding="utf-8",
    )
    (assets / "app.js").write_text("console.log('if')\n", encoding="utf-8")
    monkeypatch.setattr(main_module, "FRONTEND_DIST", dist)
    return dist


async def test_console_page_is_served_and_does_not_shadow_the_api(console_dist: Path) -> None:
    """挂载顺序契约：`/` 上的静态目录排在 API 之后，两边都得通。

    只断结构（状态码、内容类型、根节点），不断文案——Vue 页面的中文改一个字
    不该让 API 测试变红。
    """
    console_app = create_app()
    transport = ASGITransport(app=console_app)
    async with AsyncClient(transport=transport, base_url="http://if.test") as client:
        page = await client.get("/")
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert b'id="app"' in page.content

        asset = await client.get("/assets/app.js")
        assert asset.status_code == 200
        assert "javascript" in asset.headers["content-type"]

        # 前端路由深链：磁盘上没有 reports/deep-link，回退 index.html 让客户端接手。
        deep_link = await client.get("/reports/deep-link")
        assert deep_link.status_code == 200
        assert b'id="app"' in deep_link.content

        assert (await client.get("/api/v1/health")).json()["status"] == "ok"

        # API 的 404 不许被 SPA 回退吃掉：否则客户端拿到一份 HTML，分不清接口还是页面不存在。
        unknown_api = await client.get("/api/v1/no-such-endpoint")
        assert unknown_api.status_code == 404
        assert "text/html" not in unknown_api.headers["content-type"]
        assert b'id="app"' not in unknown_api.content

        # 带扩展名的缺失资源也只报 404，不能回退成 index.html（那会把 404 伪装成 200）。
        missing_asset = await client.get("/assets/missing.js")
        assert missing_asset.status_code == 404
        assert b'id="app"' not in missing_asset.content


async def test_console_is_not_mounted_when_the_directory_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目录缺席时必须**不挂载**：`StaticFiles(check_dir=True)` 会直接抛错，
    而 `create_app()` 抛错等于整个测试套件起不来。"""
    from app import main as main_module

    monkeypatch.setattr(main_module, "FRONTEND_DIST", tmp_path / "no-such-dist")

    app_without_console = create_app()
    transport = ASGITransport(app=app_without_console)
    async with AsyncClient(transport=transport, base_url="http://if.test") as client:
        assert (await client.get("/")).status_code == 404
        assert (await client.get("/reports/deep-link")).status_code == 404
        assert (await client.get("/api/v1/health")).status_code == 200
