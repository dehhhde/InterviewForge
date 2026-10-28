# ADR-0017：上传口的两道闸——大小上限进配置段、内容与扩展名必须对得上

日期：2026-10-27
状态：已决定
相关：`app/utils/file_handler.py`、`app/config.py`（`UploadParams`）、`app/exceptions/base.py`
（`PayloadTooLargeError`）、`app/api/v1/resumes.py`、`app/api/v1/jobs.py`、
`configs/{development,production,testing}.yaml`、`tests/test_api/test_upload_limits.py`、
ADR-0011（后台化——上传链路是它的入口）、ADR-0013（写入侧的 409/422 形状）、
ADR-0014（`POST /jobs/inspect-image` 的草稿链路）

## 背景

三个上传端点（`POST /resumes/upload`、`POST /jobs/upload`、`POST /jobs/inspect-image`）
都是 `await file.read()` 一把梭，没有任何大小约束；而 `app/api/v1/resumes.py` 的注释
断言"简历是页级小文件，不构成内存风险"——**没有任何代码兜这句话**。承诺做校验的
`app/utils/file_handler.py` 从写下那天起就是一行 docstring 的空壳，全仓 0 import。

另一边是类型判定：只看后缀。后缀是客户端自报的，把 zip 改名 `.pdf` 就能一路走进
`PdfParser`，把 html 改名 `.png` 就能喂给视觉模型——后者实测会换来一份"像样的 JD"，
然后以草稿的名义回到人手里（ADR-0014 的字段核对拦不住一份本来就不存在的岗位）。

这两件事同源：**入口的字节既没有上界，也没有身份**。

## 决策

| 环节 | 选择 | 被否决的方案 |
|---|---|---|
| 上限数值 | 新增 `configs/*.yaml` 的 `upload:` 段与 `UploadParams{document_bytes, image_bytes}`，代码里不写魔数 | 在 `AppParams` 加单个 `max_upload_bytes`（任务书原案）——"简历 5MB / 海报 10MB"两个数值装不进一个键 |
| 读法 | `read_capped`：按 64KB 分块累加，越线**立即抛**，后面的字节不再读 | 一次 `read()` 读完再判长度（`Content-Length` 是客户端声明的，谎报就没防护）；流式边写盘边判（已经花了磁盘，且要处理半份文件的清理） |
| 超限的状态码 | 新 `PayloadTooLargeError(AppError)` → 413，`details` 带 `file_name / limit_bytes / received_bytes` | 422（和"内容不合格"混在一个码里，客户端没法区分"重传小的"和"换文件"）；让 Starlette 截断连接（没有错误体，前端拿不到原因） |
| 类型判定 | `sniff_kind` 认 `%PDF` / PK 三种本地头 / JPEG·PNG·GIF·BM·TIFF·WEBP 签名，认不出按 `text`；`check_kind` 比对后缀期望，错配抛 422 `upload_kind_mismatch` | 拿 MIME 头判（同样是客户端自报的）；只比 `application/*` 白名单（`.txt` 本来就没有签名，会被误伤） |
| 放手不判的三种情况 | 后缀不认识、空文件、认不出签名（按 `text`）——都交回服务层既有的错误码 | 在这里也拒（`upload_bad_suffix` / `upload_empty_file` 是同一次上传的候选错误，两处各判一次会有两套标准） |
| 落点 | 端点里显式调用 `file_handler`（各 3 行） | FastAPI 依赖（依赖注入里读请求体，测试和脚本两条来路都得绕过它）；中间件（不认识"后缀该对应哪类签名"，业务判据漏进传输层） |

## 理由

1. **头几个字节是文件自己长出来的**。后缀、MIME、`Content-Length` 全是客户端声明，
   只有签名是格式的必然结果——认它才叫校验。
2. **越线即停是"代价有界"的唯一形状**。分块读到上限为止，最坏占用是一个上限大小的缓冲；
   照声明一次读完，最坏占用是客户端想给你的任何数。
3. **上限是两个数，不是一个**。图片（海报、截图）天然比 PDF 大，共用一个数就要么把
   简历放宽到 10MB，要么把海报拒在 5MB 外——后者是真实功能损失。
4. **错误码是客户端唯一能据以分支的契约**。413 与 422 分开的不是严重度，是补救动作：
   前者压缩或换小文件，后者换文件或改后缀。

## 权衡与代价

- **`read_capped` 的代价上限仍是一个完整上限大小的内存**（5MB/10MB 级）。要压到常数级
  就得流式落盘 + 失败清理，那条路的复杂度（半份文件、`stored_path` 一致性）不划算。
- **"灌 2GB 假 PDF 看 RSS"没做**。用例里造 2GB 字节本身就是自伤，判据换成了等价的形状：
  喂一个**无限假流**给 `read_capped`，断言它只读 1 块（64KB）就抛——块数与上传长度无关，
  这才是"RSS 不涨"的证据（`tests/test_api/test_upload_limits.py`）。
- **认不出签名一律按 `text`**，所以"一段二进制垃圾起名 `.txt`"过得了这一关，
  留给 `decode_text` 的编码判定去拒。这是诚实边界，写进 `sniff_kind` 的 docstring。
- **`.docx` 归 `zip`**：它本来就是 zip 容器，"魔数对不上"在这里是误报。
- **前端还没有 413 的分支**：控制台目前只会把 `error.message` 原样显示。
  已挂 `docs/TODO.md`。
- **`configs/testing.yaml` 里也加了 `upload:` 段，但它仍然不生效**（`APP_ENV` 没被 conftest
  设置，见 BACKLOG P3-3 / T-313）。本文件的用例是靠 `monkeypatch` 把上限调到 4KB 跑的。

## 后续影响

- `app/utils/file_handler.py` 不再是空壳，**T-312/EXT-8.5 的"7 个空壳模块"清单少一个**，
  剩下 6 个才是删除议题。
- 三个端点的入参处理从此有单一去处：新增上传端点（批量上传、头像）调 `read_capped` +
  `check_kind`，不要再自己 `await file.read()`。
- `PayloadTooLargeError` 是 `AppError` 家族的新码，会经 `task.error` 之外的 HTTP 错误体
  返回（上传是同步的，不进任务行），因此不受 ADR-0018 的键白名单约束——但**它的 `details`
  仍然不含路径**，`file_name` 来自客户端自己。
- 解析器侧的后缀判定（`PdfParser` 读失败、`decode_text` 编码失败）保持不变：本闸只保证
  "进得来的文件后缀与内容一致、且不巨大"，不保证"能被解析"。
