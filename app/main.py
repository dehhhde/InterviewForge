"""FastAPI 应用入口 / 生命周期管理〔阶段 2 · §5.1，M6 落地〕

`create_app()` 是工厂而不是模块级对象，测试才能反复建 app 而不共享 lifespan 状态；
模块级 `app` 只是给 `uvicorn app.main:app` 的入口。

lifespan 收尾 `dispose_engine()`：`BackgroundTasks` 与请求共享同一进程的连接池，
停机时不关池，aiosqlite 的遗留连接会让 Ctrl+C 后的退出变得含糊（WAL 会自己落盘，
这里只管池）。`get_engine()` 的单例按 URL 缓存，dispose 后下一次取用会重建，
测试之间拿它复位是安全的。

中间件只挂了 CORS。认证与限流的空壳已在 T-312（D-8 b）里删掉——留着只会让人以为
"改了改就能用"，而真正的前置（所有表加 `user_id`）在 `docs/TODO.md`；限流按计划书 §6
风险 3 的结论单机先不引入。日志的**级别与输出去向**在 lifespan 里由 `configure_logging()`
定（T-017）；"每个请求一条带 request_id 的访问日志"仍没接，决策在 `docs/TODO.md` §2.8，
若要接，新中间件从零写，而不是从空壳续。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.types import Scope

from app.api.v1 import api_router
from app.config import Settings, get_settings
from app.dependencies import dispose_engine, get_engine
from app.exceptions.handlers import register_exception_handlers
from app.utils.logging import configure_logging

#: 控制台构建产物。目录不存在时不挂载——测试用 `create_app()` 反复建 app，
#: `StaticFiles(check_dir=True)` 会因为缺目录直接抛错，把整条 API 测试拖下水。
#: 服务器只读 `dist/`，源码目录交给 Vite：`npm run build` 之后才更新页面。
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


class _SpaStaticFiles(StaticFiles):
    """静态目录 + 前端路由回退。

    产物里的 `/reports/<id>` 没有同名文件，直接交给静态目录会 404；而刷新深链必须
    还能拿到页面。只对"导航请求"回退：`api/` 前缀与带扩展名的路径照常 404，
    否则 `/api/v1/typo` 会变成一份 HTML，客户端再也分不清"接口不存在"和"页面不存在"。
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            # `StaticFiles.get_path` 在 Windows 上会把分隔符换成反斜杠（实测 1.8），
            # 判形前先统一成正斜杠。
            relative = path.replace("\\", "/").lstrip("/")
            last_segment = relative.rsplit("/", 1)[-1]
            if exc.status_code != 404 or relative.startswith("api/") or "." in last_segment:
                raise
            return await super().get_response("index.html", scope)


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # 日志先于引擎：`get_engine()` 若在此刻就抛错，那句报错得有地方落。
        configure_logging()
        # 启动即建引擎：把"连接串写错"这类事故挡在第一条请求之前，而不是埋进
        # 第一个上传请求的后台任务里。
        get_engine()
        yield
        await dispose_engine()

    app = FastAPI(title=cfg.app_name, lifespan=lifespan)
    if cfg.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    register_exception_handlers(app)
    app.include_router(api_router)
    # 挂在最后：Starlette 按注册顺序匹配，`/` 上的静态目录必须排在 API 之后，
    # 否则 `/api/v1/...` 会被它先吃掉。`html=True` 让根路径直接返回 index.html。
    if FRONTEND_DIST.is_dir():
        app.mount("/", _SpaStaticFiles(directory=FRONTEND_DIST, html=True), name="console")
    return app


app = create_app()
