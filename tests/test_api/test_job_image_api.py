"""`POST /jobs/inspect-image`：图片 → 草稿这条链路的端点测试（全程离线）。

夹具来自 `tests/test_api/conftest.py`（真 `create_app()` + 单例引擎 + `tmp_path` 里的独立库），
本文件只额外补一条视觉模型槽位。还有一处差别：这里要 `app.dependency_overrides[get_llm]`
换掉 LLM 客户端——读图与补字段都要问模型，
而替身得同时扮演视觉模型与抽取模型（`LLMClient.chat` 的 `purpose` 决定路由，两个都走
同一个 `client`，所以一个替身按顺序吐两次回复就够）。

钉的是三件事：

1. **草稿不落库**：识别完 `job` 表必须还是空的，落库只发生在人确认后的 `POST /jobs`；
2. **认不出就留空**：`missing_required` 里点名，端点不许 500、也不许编一个岗位名；
3. **没配视觉模型要显式拒绝**：文本模型"读"图不会报错，只会糊出一份像样的 JD。

断言全在状态码、`error.code`、字段值与行数上，不比模型文案。
"""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.dependencies import get_llm, get_sessionmaker
from app.models.job import Job
from app.utils.db import session_scope
from app.utils.llm_client import LLMClient
from tests.test_agents._fake_llm import FakeCompletions

_VISION_MODEL = "some-vision-model"

#: 视觉模型的回复：形状够规则层吃（岗位名、公司、年限都有字形）
_OCR_TEXT = (
    "岗位名称：后端开发工程师\n"
    "公司名称：示例科技有限公司\n"
    "任职要求：精通 Python，熟悉 Redis，3-5 年工作经验"
)
#: 故意不含岗位名的 OCR 结果，只含一个没有后缀词典可依的公司名
_OCR_TEXT_WITHOUT_TITLE = "某某电商平台 长期招聘\n任职要求：精通 Python，5 年以上经验"
#: 岗位名藏在句子里（带冒号的前缀会让标题判据退让），公司名照样认不出
_OCR_TEXT_NAME_IN_SENTENCE = (
    "某某电商平台 长期招聘\n急聘：资深后端开发工程师（可远程）\n任职要求：精通 Python，5 年以上经验"
)


@pytest.fixture(autouse=True)
def _a_vision_model_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """库与上传目录由 conftest 指好，这里只补本文件多出来的一条：视觉模型槽位。"""
    monkeypatch.setattr(get_settings(), "llm_model_vision", _VISION_MODEL)


async def _count(session: AsyncSession, model: type[Job]) -> int:
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


def _wire_llm(app: FastAPI, replies: list[str]) -> FakeCompletions:
    fake = FakeCompletions(replies)
    app.dependency_overrides[get_llm] = lambda: LLMClient(settings=get_settings(), client=fake)
    return fake


def _png(tmp_path: Path, name: str = "jd.png") -> bytes:
    """一张真图片的字节流。内容不重要：图是喂给替身的，模型不回真解析。"""
    path = tmp_path / name
    pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 600)).save(path)
    return path.read_bytes()


async def test_image_draft_fills_every_field_and_writes_nothing(
    app: FastAPI, client: AsyncClient, tmp_path: Path
) -> None:
    fake = _wire_llm(app, [_OCR_TEXT])

    response = await client.post(
        "/api/v1/jobs/inspect-image",
        files={"file": ("岗位海报.png", _png(tmp_path), "image/png")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "后端开发工程师"
    assert body["company"] == "示例科技有限公司"
    assert body["min_years"] == 3.0, "区间取下界"
    assert "python" in body["required_skills"]
    assert body["missing_required"] == []
    # 规则层把三项都捞齐了，不该再花第二次调用
    assert len(fake.calls) == 1
    assert fake.calls[0]["model"] == _VISION_MODEL, "读图必须走 vision 路由"
    async with session_scope(get_sessionmaker()) as session:
        assert await _count(session, Job) == 0, "草稿不落库"


async def test_image_draft_asks_the_model_only_for_the_missing_name(
    app: FastAPI, client: AsyncClient, tmp_path: Path
) -> None:
    """岗位名写在句子里、不带任何标签：规则层认不出，兜底认回来的是**原文里真有的**那段。"""
    payload = json.dumps(
        {
            "title": {"value": "资深后端开发工程师", "evidence": "资深后端开发工程师"},
            "company": None,
        },
        ensure_ascii=False,
    )
    fake = _wire_llm(app, [_OCR_TEXT_NAME_IN_SENTENCE, payload])

    response = await client.post(
        "/api/v1/jobs/inspect-image",
        files={"file": ("jd.png", _png(tmp_path), "image/png")},
    )

    assert response.status_code == 200
    body = response.json()
    assert len(fake.calls) == 2
    assert fake.calls[1]["model"] == get_settings().llm_model_extract
    assert body["title"] == "资深后端开发工程师"
    assert body["company"] == "", "公司名没有后缀可依，模型答 null 就留空"
    assert body["missing_required"] == [], "必填项齐了，页面不必标红"


async def test_image_draft_leaves_undetectable_fields_blank(
    app: FastAPI, client: AsyncClient, tmp_path: Path
) -> None:
    """模型答 null：岗位名留空并写进 `missing_required`，让页面去问人。"""
    _wire_llm(app, [_OCR_TEXT_WITHOUT_TITLE, json.dumps({"title": None, "company": None})])

    response = await client.post(
        "/api/v1/jobs/inspect-image",
        files={"file": ("jd.png", _png(tmp_path), "image/png")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == ""
    assert body["missing_required"] == ["title"]
    assert body["jd_text"] == _OCR_TEXT_WITHOUT_TITLE
    async with session_scope(get_sessionmaker()) as session:
        assert await _count(session, Job) == 0


async def test_hallucinated_name_is_not_filled_into_the_draft(
    app: FastAPI, client: AsyncClient, tmp_path: Path
) -> None:
    """模型给的证据在原文里找不到 = 幻觉，草稿的岗位名照样是空的。"""
    payload = json.dumps(
        {"title": {"value": "首席架构师", "evidence": "本公司诚聘首席架构师"}},
        ensure_ascii=False,
    )
    _wire_llm(app, [_OCR_TEXT_WITHOUT_TITLE, payload])

    response = await client.post(
        "/api/v1/jobs/inspect-image",
        files={"file": ("jd.png", _png(tmp_path), "image/png")},
    )

    assert response.status_code == 200
    assert response.json()["missing_required"] == ["title"]


@pytest.mark.parametrize(
    ("file_name", "payload", "code"),
    [
        ("jd.txt", b"plain text jd", "job_image_bad_suffix"),
        ("jd.pdf", b"%PDF-1.4\n% stub\n%%EOF\n", "job_image_bad_suffix"),
        ("jd.png", b"", "jd_empty_file"),
    ],
)
async def test_refusals_leave_no_row(
    app: FastAPI,
    client: AsyncClient,
    tmp_path: Path,
    file_name: str,
    payload: bytes,
    code: str,
) -> None:
    fake = _wire_llm(app, [_OCR_TEXT])

    response = await client.post(
        "/api/v1/jobs/inspect-image",
        files={"file": (file_name, payload, "application/octet-stream")},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == code
    assert fake.calls == [], "后缀与空文件挡在模型调用之前"
    async with session_scope(get_sessionmaker()) as session:
        assert await _count(session, Job) == 0


async def test_unreadable_image_is_422(
    app: FastAPI, client: AsyncClient, tmp_path: Path
) -> None:
    """视觉模型一个字都没读出来：这是内容问题，不是网关问题，返 422 不是 502。"""
    _wire_llm(app, [""])

    response = await client.post(
        "/api/v1/jobs/inspect-image",
        files={"file": ("jd.png", _png(tmp_path), "image/png")},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "parse_vision_empty"


async def test_spaced_latin_in_ocr_still_reaches_the_skill_dictionary(
    app: FastAPI, client: AsyncClient, tmp_path: Path
) -> None:
    """模型把字母逐个加空格时（实测 `P y t h o n`），OCR 层先压回词形，技能才认得出〔T-104〕。

    判据打在下游消费点上：`required_skills` 是 `match` 的输入，漏一项等于少一条缺口判定。
    """
    _wire_llm(
        app,
        [
            "岗位名称：后端开发工程师\n"
            "公司名称：示例科技有限公司\n"
            "任职要求：精通 P y t h o n，熟悉 R e d i s，3-5 年工作经验"
        ],
    )

    response = await client.post(
        "/api/v1/jobs/inspect-image",
        files={"file": ("jd.png", _png(tmp_path), "image/png")},
    )

    assert response.status_code == 200
    body = response.json()
    assert {"python", "redis"} <= set(body["required_skills"])
    assert "P y t h o n" not in body["jd_text"], "正文里不该留下逐字空格的形状"


async def test_blank_poster_fails_as_empty_read_not_as_meta_prose(
    app: FastAPI, client: AsyncClient, tmp_path: Path
) -> None:
    """空白海报换来的是模型自己的话（实测 14 字），它不该变成 `jd_text`〔T-104 / TODO §2.6〕。

    卡片给的形状是"草稿 `jd_text` 留空 + notes 记一条"，实现走的是既有失败分支
    `parse_vision_empty`（422）：空正文的草稿存不进 `POST /jobs`（`jd_text` 有 5 字下限），
    留一张空表单只是把失败推迟到保存那一刻。
    """
    fake = _wire_llm(app, ["（图片中未出现可识别的文字）"])

    response = await client.post(
        "/api/v1/jobs/inspect-image",
        files={"file": ("jd.png", _png(tmp_path), "image/png")},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "parse_vision_empty"
    assert len(fake.calls) == 1, "读图那一次照常花掉，但没有第二次补字段的钱"


async def test_without_a_vision_model_it_refuses_to_guess(
    app: FastAPI, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """退回纯文本模型不会报错，只会编出一份看似完整的 JD——所以必须当场拒。"""
    monkeypatch.setattr(get_settings(), "llm_model_vision", "")
    fake = _wire_llm(app, [_OCR_TEXT])

    response = await client.post(
        "/api/v1/jobs/inspect-image",
        files={"file": ("jd.png", _png(tmp_path), "image/png")},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "parse_vision_unconfigured"
    assert fake.calls == []
