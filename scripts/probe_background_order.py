"""探针：`BackgroundTasks` 与 `get_db` 收尾谁先跑〔M6 · ADR-0011〕

结论决定 M6 的形状，所以把它做成能反复跑的脚本，而不是一句"我记得 FastAPI 是这么工作的"：

- 若 `background` 在 `dep-exit` **之后**：请求会话的提交早于后台开跑，端点可以继续用
  `SessionDep`，服务层只 flush；
- 若在**之前**（实测结果，FastAPI 0.143 + Starlette 1.8）：后台读不到未提交的行，
  凡是"排后台任务"的端点都必须自己开事务、在返回前提交（`dependencies.SessionMakerDep`）。

跑法：`.venv/Scripts/python.exe -X utf8 scripts/probe_background_order.py`
"""

import asyncio
from collections import deque
from collections.abc import AsyncIterator

from fastapi import BackgroundTasks, Depends, FastAPI
from httpx import ASGITransport, AsyncClient

order: deque[str] = deque()


async def get_db() -> AsyncIterator[str]:
    """形状与真实依赖一致：生成器 + 收尾动作（真实版在这里 commit）。"""
    order.append("dep-enter")
    yield "session"
    order.append("dep-exit")


async def background() -> None:
    order.append("background")


def build_app() -> FastAPI:
    api = FastAPI()

    @api.post("/x")
    async def endpoint(tasks: BackgroundTasks, session: str = Depends(get_db)) -> dict[str, bool]:
        assert session == "session"
        tasks.add_task(background)
        order.append("endpoint")
        return {"ok": True}

    return api


async def main() -> None:
    transport = ASGITransport(app=build_app())
    async with AsyncClient(transport=transport, base_url="http://probe") as client:
        response = await client.post("/x")
    sequence = list(order)
    print("fastapi/starlette/httpx 顺序：", " → ".join(sequence))
    print("响应状态：", response.status_code)
    if sequence.index("background") < sequence.index("dep-exit"):
        print("判定：后台早于依赖收尾 → 排后台任务的端点必须自己提交（SessionMakerDep）")
    else:
        print("判定：后台晚于依赖收尾 → 可以继续用 get_db 的提交")


if __name__ == "__main__":
    asyncio.run(main())
