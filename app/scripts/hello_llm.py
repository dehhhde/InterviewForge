"""LLM 连通性自检：一次极小调用，打印路由到的模型与 token 用量〔阶段 0 · §3.5〕

这个文件在 2026-10-27 之前**只有一行 docstring**：`python -m app.scripts.hello_llm` 什么都不做、
退出码 0。空壳里最坏的就是这种"静默成功"——新人照着 README 跑完以为环境通了，实际上一条请求
都没发出去，于是第一次真跑在工作流中间才炸。现在它做 docstring 承诺的那件事。

与 `live_check.py` 的分工（两者都要联网、都手动跑、都不进 `pytest`）：

- 本脚本答"密钥、base_url、模型别名这三样对不对"，1 次调用，`max_tokens` 掐在 16 上下，
  成本几分钱。
- `live_check.py` 答"整条 parse+validate 在真简历上跑出什么"，2 次调用、约 7k token，
  还会把反思响应录成回放 fixture。

跑法（`PYTHONPATH=.` 不能省，`app.scripts` 不是已安装包）：

    PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/Scripts/python.exe -X utf8 -m app.scripts.hello_llm

**异常一律原样抛**，这正是自检的全部价值：401 = key 不对，402 = 账户余额不足，
404 / "模型不存在" = `LLM_MODEL_*` 填了上游没有的别名。吞掉它们就等于回到那个静默空壳。
"""

from __future__ import annotations

import asyncio
import json

from app.config import get_settings
from app.utils.llm_client import LLMClient

#: 自检只要求"模型能回话"。16 个 token 够它吐一个词，也把成本钉死在上限之内——
#: 这个脚本可能被人反复跑，不该一次烧掉一页简历的量。
_MAX_TOKENS = 16

#: 一句与业务无关的话：内容本身不参与任何判断，能回来就算通。
_PING = "只回一个字：好"


async def run(llm: LLMClient) -> str:
    """打一次调用，返回模型应答原文。用量累计留在 `llm.usage` 里，由调用方打印。"""
    return await llm.chat(
        "extract",
        [{"role": "user", "content": _PING}],
        max_tokens=_MAX_TOKENS,
        temperature=0.0,
    )


async def main(llm: LLMClient | None = None) -> None:
    client = llm or LLMClient(get_settings())
    cfg = client.settings
    try:
        model = client.model_for("extract")
        print(f"== 请求模型：{model}（base_url={cfg.deepseek_base_url}）")
        reply = await run(client)
        print(f"== 应答：{reply.strip()!r}")
        print(f"== 用量：{json.dumps(client.usage.per_model, ensure_ascii=False)}")
    finally:
        # 关掉连接池：`main` 是进程出口，但 `run`/`main` 也会被别的脚本或测试直接调用，
        # 不关就把一台 AsyncOpenAI 的池子吊在调用方生命周期上（同 T-011 的理由）。
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
