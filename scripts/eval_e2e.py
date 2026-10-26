"""端到端评测：可用率 / 幻觉率 / token 成本〔阶段 9 · §12.4，M6〕

**要联网、要花 token 的手动脚本，不在 `pytest` 里跑**（同 `app/scripts/live_check.py`）。
它测的是整条产品路径，所以走生产接缝而不是另搭一套：

    上传（`resume_service.accept_upload`）→ 建报告头（`interview_service.start_generation`）
    → 后台跑图（`run_question_generation`）→ 从终态算三个指标

三个指标的口径都是**机械判据**，不看模型文案，所以换模型、改 prompt 之后数字仍然可比：

- **可用率** = 通过重验的题目数 / 配额表计划题数。重验 = 再过一遍 `QuestionSchema`
  ＋ 引用按来源逐字可溯（`evidence_problem`，与出题/评审同一个判据）＋ 题干不重复。
  分母用"计划题数"而不是"实发题数"：少出题本身就是不可用，按实发算会把它读成满分。
- **幻觉率** = 声称有出处（`source=resume`/`jd`）却核不上原文的题目数 / 实发题目数。
  理想值是 0——`generate` 的闸门本该把这类题整题丢弃，所以**非 0 说明有题绕过了闸门**
  （外部塞题、分支回归）。这个指标的意义就在于它本该一直是 0。
- **token 成本** = 每次运行的 prompt + completion tokens（`llm_client.UsageTotals` 累计），
  顺带报每题均价与有没有撞上 `agent.max_tokens_per_run`。撞护栏的样本算失败样本：成本与
  产出要一起看，否则"零幻觉"很可能只是"没跑完"。

跑法（`.env` 里要有 `DEEPSEEK_API_KEY`，先跑 seed 备好岗位）：

    .venv/Scripts/python.exe -X utf8 scripts/eval_e2e.py [简历目录] [JD 文件]

计划书按"10 份样本简历"验收；仓库目前只有 1 份真简历，样本不足时脚本会显式警告，
欠账见 `docs/TODO.md`。写入开发库 `data/interview_forge.db`（与 `run_e2e_live.py` 一致），
每份样本新建 `resume`/`interview` 行，不覆盖历史。
"""

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.dependencies import get_sessionmaker  # noqa: E402
from app.exceptions.base import AppError  # noqa: E402
from app.parsers.image_parser import IMAGE_SUFFIXES  # noqa: E402
from app.repositories.job_repository import JobRepository  # noqa: E402
from app.schemas.question import QuestionSchema  # noqa: E402
from app.services import interview_service, resume_service  # noqa: E402
from app.services.question_service import (  # noqa: E402
    evidence_problem,
    order_questions,
    planned_count,
    to_row,
)
from app.tasks.question_generation import run_question_generation  # noqa: E402
from app.utils.db import session_scope  # noqa: E402
from app.utils.llm_client import LLMClient  # noqa: E402

#: 目标样本数（计划书 M6 的验收口径）
TARGET_SAMPLES = 10

#: 会声称出处的来源。`generic` 不在列：它不指认原文，也就无所谓"编造出处"
_CITED_SOURCES = ("resume", "jd")


@dataclass(frozen=True)
class SampleResult:
    """一份简历跑完之后的全部数字。失败样本只留错误码，不参与分子分母。"""

    name: str
    planned: int = 0
    usable: int = 0
    emitted: int = 0
    hallucinated: int = 0
    duplicates: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    error: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def ok(self) -> bool:
        return not self.error


def _sample_files(directory: Path) -> list[Path]:
    """目录里可解析的简历。扩展名集合与上传入口一致，评测不该测产品不收的文件。"""
    supported = {".pdf", ".docx", *IMAGE_SUFFIXES}
    files = sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in supported
    )
    if not files:
        raise SystemExit(f"{directory} 里没有可解析的简历（支持 {sorted(supported)}）")
    has_image = any(path.suffix.lower() in IMAGE_SUFFIXES for path in files)
    if has_image and not get_settings().llm_model_vision:
        raise SystemExit("目录里有图片简历，但 `llm_model_vision` 未配置：先删图或配模型")
    return files


def _jd_override(arg: str | None) -> str | None:
    """命令行给的 JD：是文件就读文本，否则整段当 JD。同步函数——`is_file()` 是阻塞 IO。"""
    if not arg:
        return None
    path = Path(arg)
    return path.read_text(encoding="utf-8") if path.is_file() else arg


async def _pick_job(jd_arg: str | None) -> tuple[str, str, str]:
    """返回 `(job_id, jd_text, 岗位名)`。给了 JD 覆盖就用它，但岗位仍取库里第一个——
    `interview.job_id` 要有真行可指，而评测要的是可重复，不是多样。"""
    async with session_scope(get_sessionmaker()) as session:
        jobs = await JobRepository(session).list()
    if not jobs:
        raise SystemExit("库里没有岗位，先跑 app.scripts.seed")
    job = jobs[0]
    label = f"{job.title}@{job.company or '-'}"
    override = _jd_override(jd_arg)
    if override is None:
        return str(job.id), str(job.jd_text), label
    return str(job.id), override, f"{label}（JD 由命令行覆盖）"


def _shape_ok(question: dict[str, Any]) -> bool:
    """落库形状再过一遍 `QuestionSchema`：读端就是这么校验的，闸门漏了会在这里露。"""
    try:
        QuestionSchema.model_validate(to_row(question))
    except Exception:  # noqa: BLE001 —— 评测要把"任何形状问题"都算成不可用，不分类
        return False
    return True


def _score(state: dict[str, Any]) -> tuple[int, int, int, int]:
    """重验终态题目。返回（可用数、实发数、幻觉数、重复数）。

    三个判据合成一个"可用"：形状合格、引用核得上、题干没在前面出现过。
    重复只算**后出现的那一道**——第一道是正常题，第二道才是换皮重复。
    """
    raw_text = str(state.get("raw_text") or "")
    jd_text = str(state.get("jd_text") or "")
    ordered = order_questions(state.get("questions") or [])

    usable = 0
    hallucinated = 0
    duplicates = 0
    seen: set[str] = set()
    for question in ordered:
        source = str(question.get("source") or "")
        evidence = str(question.get("evidence") or "")
        problem = evidence_problem(source, evidence, raw_text=raw_text, jd_text=jd_text)
        if source in _CITED_SOURCES and problem:
            hallucinated += 1
        stem = str(question.get("stem") or "").strip()
        repeated = stem in seen
        seen.add(stem)
        duplicates += int(repeated)
        if _shape_ok(question) and not problem and not repeated:
            usable += 1
    return usable, len(ordered), hallucinated, duplicates


async def _run_sample(pdf: Path, job_id: str, jd_text: str) -> SampleResult:
    """一份简历走完"上传 → 建头 → 后台跑图"，把终态折成数字。"""
    maker = get_sessionmaker()
    client = LLMClient()
    # 读盘放线程里：`accept_upload` 落盘也走线程，评测脚本不该抱着磁盘等（ruff ASYNC240）
    data = await asyncio.to_thread(pdf.read_bytes)
    resume, _parse_task = await resume_service.accept_upload(
        maker, file_name=pdf.name, data=data
    )
    head, task, _jd = await interview_service.start_generation(
        maker, resume_id=resume.id, job_id=job_id
    )
    outcome = await run_question_generation(
        task.id, resume.id, head.id, jd_text, llm=client, sessionmaker=maker
    )
    usage = client.usage
    measured = {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "calls": usage.calls,
    }
    if not outcome.get("ok"):
        error = outcome.get("error")
        code = error.code if isinstance(error, AppError) else type(error).__name__
        return SampleResult(name=pdf.name, error=str(code), **measured)

    state = outcome.get("state") or {}
    items = (state.get("plan") or {}).get("items") or []
    planned = planned_count(items) or get_settings().question.total
    usable, emitted, hallucinated, duplicates = _score(state)
    return SampleResult(
        name=pdf.name,
        planned=planned,
        usable=usable,
        emitted=emitted,
        hallucinated=hallucinated,
        duplicates=duplicates,
        **measured,
    )


def _print_sample(result: SampleResult) -> None:
    if not result.ok:
        print(f"  ✗ {result.name:<30} 失败：{result.error}  tokens={result.total_tokens:,}")
        return
    print(
        f"  · {result.name:<30} 可用 {result.usable}/{result.planned}"
        f"  幻觉 {result.hallucinated}/{result.emitted}  重复 {result.duplicates}"
        f"  tokens={result.total_tokens:,}（{result.calls} 次调用）"
    )


def _ratio(numerator: int, denominator: int, label: str) -> str:
    if not denominator:
        return f"{label}：分母为 0（没有题可算）"
    return f"{label}：{numerator}/{denominator} = {numerator / denominator:.2%}"


def _report(results: list[SampleResult], *, minutes: float) -> None:
    done = [item for item in results if item.ok]
    failed = [item for item in results if not item.ok]
    budget_hits = [item for item in failed if item.error == "cost_budget_exceeded"]

    planned = sum(item.planned for item in done)
    usable = sum(item.usable for item in done)
    emitted = sum(item.emitted for item in done)
    hallucinated = sum(item.hallucinated for item in done)
    prompt = sum(item.prompt_tokens for item in results)
    completion = sum(item.completion_tokens for item in results)
    total = prompt + completion
    runs = len(results) or 1
    limit = get_settings().agent.max_tokens_per_run

    print("\n=== 三个指标 ===")
    summary = f"样本：{len(results)} 份（成功 {len(done)}，失败 {len(failed)}）"
    print(f"{summary}，耗时 {minutes:.1f} 分钟")
    print(_ratio(usable, planned, "可用率"))
    print(_ratio(hallucinated, emitted, "幻觉率"))
    print(f"token 成本：总计 {total:,}（prompt {prompt:,} + completion {completion:,}）")
    print(f"           平均每次运行 {total // runs:,} tokens")
    print(f"           单次运行护栏 {limit:,} tokens，撞线 {len(budget_hits)} 份")
    if emitted:
        print(f"           平均每道题 {total / emitted:,.0f} tokens")
    for item in failed:
        print(f"  失败样本：{item.name} → {item.error}")


def _resolve_args() -> tuple[Path, str | None]:
    """参数校验放在同步函数里：`is_dir()` 是阻塞 IO，不该出现在 async 函数体内。"""
    directory = Path(sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/resumes")
    if not directory.is_dir():
        raise SystemExit(f"样本目录不存在：{directory}")
    return directory, (sys.argv[2] if len(sys.argv) > 2 else None)


async def main() -> int:
    directory, jd_arg = _resolve_args()
    files = _sample_files(directory)
    job_id, jd_text, label = await _pick_job(jd_arg)

    if len(files) < TARGET_SAMPLES:
        print(
            f"警告：样本只有 {len(files)} 份，计划书按 {TARGET_SAMPLES} 份验收；"
            "数字仍算，但代表性不足（欠账见 docs/TODO.md）"
        )
    print(f"岗位：{label}  JD 前 40 字：{jd_text[:40]!r}")

    loop = asyncio.get_running_loop()
    started = loop.time()
    results: list[SampleResult] = []
    for pdf in files:
        print(f"跑样本：{pdf.name}")
        results.append(await _run_sample(pdf, job_id, jd_text))
        _print_sample(results[-1])
    _report(results, minutes=(loop.time() - started) / 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
