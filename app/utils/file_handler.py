"""上传读取的兜底：大小上限 + 魔数识别〔阶段 4 · §7.5，BACKLOG P0-2 / TASK_PLAN T-002〕

补的是两个相邻的缺口：`await file.read()` 无界，一个 2GB 的假 PDF 就能把进程打趴；
而扩展名是客户端自报的，把 zip 改名成 `.pdf` 就能一路走进解析器。
头几个字节是文件自己长出来的，认它才叫校验。

分层：这里不碰数据库、不认识业务行，只把"一个 `UploadFile`"变成"一段有上限、
内容对得上所声称类型的字节"。上限数值来自 `configs/*.yaml` 的 `upload:` 段，
代码里不写魔数（`AGENTS.md` §3）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import UploadFile

from app.config import Settings, get_settings
from app.exceptions.base import PayloadTooLargeError, ValidationFailedError
from app.parsers.image_parser import IMAGE_SUFFIXES

FileKind = Literal["pdf", "zip", "image", "text"]

#: 分块读的块大小。为什么不"照着上限一次读完"：`Content-Length` 是客户端声明的，
#: 谎报成 40 字节、真身 2GB 的流只能按块推进时才知道有多长——累加到越线即停，
#: 才是"既不轻信声明、又不至于先把整份读进内存"的那个形状。
_CHUNK_BYTES = 64 * 1024

#: 后缀 -> 应当认出的签名类别。`.docx` 归 `zip`：它本来就是 zip 容器，
#: 拿 Office 的那串标识去比是比不上的。
_SUFFIX_KINDS: dict[str, FileKind] = {
    ".pdf": "pdf",
    ".docx": "zip",
    ".txt": "text",
    ".md": "text",
    **dict.fromkeys(IMAGE_SUFFIXES, "image"),
}


def sniff_kind(data: bytes) -> FileKind:
    """按文件头认类型。认不出的一律按 `text`：`.txt` / `.md` 本来就没有签名可看。

    这是魔数校验的诚实边界——它挡得住"zip 改名成 pdf"，挡不住"一段二进制垃圾起名 .txt"，
    后者交给 `decode_text` 的编码判定去拒（`job_service`）。
    """
    if data.startswith(b"%PDF"):
        return "pdf"
    # zip 的三种本地头：正常条目、空归档、跨卷
    if data[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    if _looks_like_image(data):
        return "image"
    return "text"


def _looks_like_image(data: bytes) -> bool:
    if data.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF8", b"BM")):
        return True
    # TIFF 的字节序标记（小端 / 大端）
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return True
    # webp：`RIFF` 只是容器，`WEBP` 标记在第 9-12 字节，缺了它就不是图
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def limit_bytes_for(file_name: str, settings: Settings | None = None) -> int:
    """按后缀挑上限：图片走 `image_bytes`，其余走 `document_bytes`。

    未知后缀不在这里拒——"支不支持这种类型"是服务层既有的判据（`upload_bad_suffix` /
    `jd_bad_suffix`），这里只回答"能有多大"；同一次上传不该有两个候选错误码。
    """
    cfg = settings or get_settings()
    if _SUFFIX_KINDS.get(Path(file_name).suffix.lower()) == "image":
        return cfg.upload.image_bytes
    return cfg.upload.document_bytes


async def read_capped(
    upload: UploadFile, *, file_name: str | None = None, settings: Settings | None = None
) -> bytes:
    """分块读到上限为止，越线就抛 413。返回的是已经完整落在上限内的字节。

    `file_name` 缺省取 `upload.filename`：调用点是 `UploadFile`，但服务层的测试与脚本
    可能拿着一个已经填好名字的普通对象，两处用同一个判据挑上限。
    """
    cfg = settings or get_settings()
    name = file_name if file_name is not None else (upload.filename or "")
    limit = limit_bytes_for(name, cfg)

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            # 到此为止：后面的字节根本不读了，所以越线的代价是一份上限大小的缓冲，
            # 而不是整份上传。
            raise PayloadTooLargeError(
                "上传的文件超过大小上限",
                details={"file_name": name, "limit_bytes": limit, "received_bytes": total},
            )
        chunks.append(chunk)
    return b"".join(chunks)


def check_kind(data: bytes, *, file_name: str) -> None:
    """内容签名必须对得上扩展名声称的类型，对不上就 422。

    三处放手不判，都是为了不把服务层已有的错误码抢过来：
    - 后缀不认识（上面那条理由）；
    - 空文件，那是 `upload_empty_file` / `jd_empty_file` 的活儿；
    - 认不出签名的按 `text` 处理，所以 `.txt` / `.md` 只判"别是 pdf/zip/图片"。
    """
    expected = _SUFFIX_KINDS.get(Path(file_name).suffix.lower())
    if expected is None or not data:
        return
    found = sniff_kind(data)
    if found != expected:
        raise ValidationFailedError(
            "文件内容与扩展名对不上",
            code="upload_kind_mismatch",
            details={"file_name": file_name, "expected": expected, "found": found},
        )
