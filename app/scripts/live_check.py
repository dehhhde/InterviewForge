"""真调冒烟：真简历走 parse + validate，打印通道变化并把反思响应录成回放 fixture。

与 `hello_llm.py` 同一类东西——**要联网、要花 token 的手动脚本**，不在 `pytest` 里跑
（`pyproject.toml` 的 coverage 也已把 `app/scripts/*` 排除）。一次运行 2 次调用，
按真简历实测约 6.5k prompt + 0.4k completion token。跑法：

    PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/Scripts/python.exe app/scripts/live_check.py

`.env` 里要有 `DEEPSEEK_API_KEY`。录出来的 `tests/fixtures/llm/validate_liang_qingsheng.json`
只被回放测试读取它的 `response` 字段，所以重跑就是覆盖标准答案——改 prompt 前后各跑一次
才能看出差别，别把它当回归基线自动刷。
"""

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from openai import AsyncOpenAI

from app.agents.nodes.parse_node import make_parse_node
from app.agents.nodes.validate_node import make_validate_node
from app.config import get_settings
from app.prompts.registry import get_prompt
from app.utils.llm_client import LLMClient

REAL_RESUME = Path("tests/fixtures/resumes/附件简历-梁清生.pdf")
FIXTURE = Path("tests/fixtures/llm/validate_liang_qingsheng.json")


class _Recorder:
    """包一层真 completions，把每次的模型与原始响应记下来。"""

    def __init__(self, real: Any) -> None:
        self.real = real
        self.records: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        resp = await self.real.create(**kwargs)
        self.records.append(
            {"model": kwargs.get("model"), "response": resp.choices[0].message.content}
        )
        return resp


def _record(reflection: dict[str, Any], raw_text: str, usage: dict[str, Any]) -> None:
    prompt = get_prompt("validate")
    FIXTURE.write_text(
        json.dumps(
            {
                "node": prompt.node,
                "prompt_version": prompt.version,
                "purpose": "extract",
                "model": reflection["model"],
                "raw_text_chars": len(raw_text),
                "response": reflection["response"],
                "usage": {
                    m: {"prompt": v["prompt"], "completion": v["completion"], "calls": v["calls"]}
                    for m, v in usage.items()
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


async def main() -> None:
    settings = get_settings()
    real = AsyncOpenAI(
        api_key=settings.deepseek_api_key.get_secret_value(),
        base_url=settings.deepseek_base_url,
        timeout=settings.llm.request_timeout_seconds,
    )
    recorder = _Recorder(real.chat.completions)
    proxy = SimpleNamespace(chat=SimpleNamespace(completions=recorder))
    llm = LLMClient(settings=settings, client=proxy)

    workdir = Path(tempfile.mkdtemp(prefix="if_live_"))
    shutil.copyfile(REAL_RESUME, workdir / "live.pdf")
    print(f"== 样本：{REAL_RESUME.name} 挂载为 live.pdf")
    try:
        parse = make_parse_node(llm=llm, settings=settings, upload_dir=workdir)
        first = await parse({"resume_id": "live"})
        parsed = first["parsed"]
        calls = len(recorder.records)
        print(f"== parse: raw_text {len(first['raw_text'])} 字 | LLM 调用 {calls} 次")
        print(
            f"   name={parsed['name']} phone={parsed['phone']} years={parsed['years_experience']}"
        )
        print(
            f"   skills={len(parsed['skills'])} work={len(parsed['work_experiences'])} "
            f"edu={len(parsed['educations'])} projects={len(parsed['projects'])}"
        )
        print(f"   errors={first['errors']} prompt_version={first.get('prompt_version', '')}")

        before = len(recorder.records)
        validate = make_validate_node(llm=llm, settings=settings)
        second = await validate({"raw_text": first["raw_text"], "parsed": parsed})
        print(f"== validate: LLM 调用 {len(recorder.records) - before} 次")
        for issue in second["validate_issues"]:
            print(f"   - [规则] {issue}")
        for issue in second.get("reflect_issues", []):
            print(f"   - [反思/只观测] {issue}")
        print(
            f"   repairs={second.get('errors', [])} retries={second.get('validate_retries', 0)} "
            f"prompt_version={second.get('prompt_version', '')}"
        )
        print(f"   parsed 是否被改动：{second['parsed'] == parsed}")
        print("== token 用量：", json.dumps(llm.usage.per_model, ensure_ascii=False))

        _record(recorder.records[-1], first["raw_text"], llm.usage.per_model)
        print(f"== 已录制 -> {FIXTURE}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
