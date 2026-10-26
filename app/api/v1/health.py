"""GET /health〔阶段 2 · §5.1，M6 落地〕

`GET /metrics` 仍属观测面选型（Langfuse 已按"无 Docker"策略移除），欠账在
`docs/TODO.md`，这里不预写空壳端点。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    cfg = get_settings()
    return {"status": "ok", "app": cfg.app_name, "env": cfg.app_env}
