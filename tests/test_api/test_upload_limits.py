"""上传兜底：大小上限 + 魔数校验的端点测试〔T-002 / BACKLOG P0-2〕

三件事，全在"落盘与建行之前"这一层：

1. 超过 `upload.document_bytes` / `upload.image_bytes` → 413；
2. 内容与扩展名对不上（zip 改名 `.pdf`）→ 422，脏文件不进解析器；
3. 被拒的上传不留痕：`resume` / `job` 表空，`upload_dir` 里没有落盘文件。

上限数值不在这里测（那是配置）。真简历 ~200KB，用例也不造 2GB：越线行为与数值大小无关，
把上限调到 4KB 用小文件就能测同一条判据。"不读完整个流"另有一条喂无限假流的用例顶着
——那才是"RSS 不涨"的判据（`Content-Length` 由客户端声明，信不得）。

断言只落状态码、`error.code`、行数与字节长度，不比错误文案。
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.api.v1 import resumes as resumes_api
from app.config import get_settings
from app.dependencies import get_llm, get_sessionmaker
from app.exceptions.base import PayloadTooLargeError, ValidationFailedError
from app.models.job import Job
from app.models.resume import Resume
from app.utils import file_handler
from app.utils.db import session_scope
from app.utils.llm_client import LLMClient

from ._stubs import PDF_BYTES

#: zip 的本地文件头：把它起名 `.pdf` 就该在魔数这一关被拒
ZIP_BYTES = b"PK\x03\x04" + b"\x00" * 60
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60
#: 比 `document_bytes`(4096) 大、比 `image_bytes`(2048) 大，两者都该撞线
_OVER_LIMIT = b"%PDF" + b"x" * 5000


@pytest.fixture(autouse=True)
def _ceilings_pulled_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """库与上传目录由 conftest 的 autouse 指好了，这里只把两个上限调小。

    上限调小不是为了"测小文件能过"，是为了不必造 5MB 字节就能撞到同一条判据。
    """
    settings = get_settings()
    monkeypatch.setattr(settings.upload, "document_bytes", 4096)
    monkeypatch.setattr(settings.upload, "image_bytes", 2048)


def _fake_llm() -> LLMClient:
    """不会被打到的客户端：`LLMDep` 每请求新建，构造本身不发音任何请求。"""

    class _Never:
        async def create(self, **kwargs: Any) -> Any:
            raise AssertionError("被大小/魔数拒掉的上传不该打到模型")

    settings = get_settings().model_copy(deep=True)
    return LLMClient(settings=settings, client=_Never())


@pytest.fixture
def app_overrides() -> dict[Any, Any]:
    """覆盖 conftest 的钩子：`inspect-image` 的依赖里要建 LLM 客户端，
    而 `.env` 里有没有密钥不该左右本文件。"""
    return {get_llm: _fake_llm}


async def _count(model: type) -> int:
    async with session_scope(get_sessionmaker()) as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


# --- 大小上限 ---


async def test_oversized_resume_upload_is_rejected_with_413(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/resumes/upload", files={"file": ("big.pdf", _OVER_LIMIT, "application/pdf")}
    )

    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "upload_too_large"
    assert error["details"]["limit_bytes"] == 4096
    # 拒在落盘与建行之前：两张表都不该因为一次失败的上传而动
    assert await _count(Resume) == 0


async def test_oversized_image_upload_uses_the_image_ceiling(client: AsyncClient) -> None:
    """上限按后缀挑：同一份字节在 `.pdf` 与 `.png` 上是两个判据（图片段更小）。"""
    data = PNG_BYTES + b"\x00" * 3000  # 3068 字节：过 `image_bytes`，远不到 `document_bytes`

    image = await client.post(
        "/api/v1/jobs/inspect-image", files={"file": ("poster.png", data, "image/png")}
    )
    assert image.status_code == 413
    assert image.json()["error"]["details"]["limit_bytes"] == 2048
    assert await _count(Job) == 0


# --- 魔数校验 ---


async def test_a_zip_renamed_to_pdf_is_rejected_with_422(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/resumes/upload", files={"file": ("renamed.pdf", ZIP_BYTES, "application/pdf")}
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "upload_kind_mismatch"
    assert error["details"]["expected"] == "pdf"
    assert error["details"]["found"] == "zip"
    assert await _count(Resume) == 0


async def test_a_pdf_renamed_to_png_is_rejected_too(client: AsyncClient) -> None:
    """反方向一样拒：图片链路会把字节交给视觉模型，一份 PDF 只会糊出一份像样的 JD。"""
    response = await client.post(
        "/api/v1/jobs/inspect-image", files={"file": ("poster.png", PDF_BYTES, "image/png")}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "upload_kind_mismatch"


# --- 正常路径 ---


async def test_a_small_real_pdf_still_gets_staged_and_scheduled(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """413/422 不能顺手把正常链路也拒了：202 + 一行简历 + 一个落盘文件。"""

    async def noop_task(task_id: str, resume_id: str) -> None:
        return None

    monkeypatch.setattr(resumes_api, "run_resume_parsing", noop_task)

    response = await client.post(
        "/api/v1/resumes/upload", files={"file": ("ok.pdf", PDF_BYTES, "application/pdf")}
    )

    assert response.status_code == 202
    assert await _count(Resume) == 1
    staged = list((get_settings().upload_dir).glob("*.pdf"))
    assert len(staged) == 1 and staged[0].stat().st_size == len(PDF_BYTES)


# --- `read_capped` 早停：越线之后一个字节都不再读 ---


class _EndlessUpload:
    """只用到 `read_capped` 认的那两样：`filename` 与分块 `read(n)`。

    它按客户端谎报的形状一直给字节，用来证明"读了几块就停"——真 2GB 的流不该出现在用例里。
    """

    def __init__(self, name: str) -> None:
        self.filename = name
        self.reads = 0

    async def read(self, size: int = -1) -> bytes:
        self.reads += 1
        return b"x" * (64 * 1024 if size < 0 else size)


async def test_read_capped_stops_at_the_line_instead_of_finishing_the_stream() -> None:
    upload = _EndlessUpload("huge.pdf")

    with pytest.raises(PayloadTooLargeError) as excinfo:
        await file_handler.read_capped(upload)  # type: ignore[arg-type]

    # 上限 4096 < 一块 64KB ⇒ 第一块就撞线。块数有界是"内存不随上传长度涨"的全部证据。
    assert upload.reads == 1
    assert excinfo.value.details["limit_bytes"] == 4096
    assert excinfo.value.details["received_bytes"] == 64 * 1024


def test_sniff_kind_recognises_the_signatures_the_endpoints_rely_on() -> None:
    """认不出签名一律按 `text`：`.txt` / `.md` 本来就没有魔数可看，不因此在上传口被拒。"""
    assert file_handler.sniff_kind(PDF_BYTES) == "pdf"
    assert file_handler.sniff_kind(ZIP_BYTES) == "zip"
    assert file_handler.sniff_kind(PNG_BYTES) == "image"
    assert file_handler.sniff_kind(b"job duties: write Python") == "text"


def test_check_kind_lets_unknown_suffixes_through() -> None:
    """后缀不认识与空文件都放手：前者是 `upload_bad_suffix` 的活儿，后者是服务层的错误码。"""
    file_handler.check_kind(ZIP_BYTES, file_name="resume.docx")  # docx 本来就是 zip 容器
    file_handler.check_kind(b"", file_name="resume.pdf")
    with pytest.raises(ValidationFailedError):
        file_handler.check_kind(ZIP_BYTES, file_name="resume.pdf")
