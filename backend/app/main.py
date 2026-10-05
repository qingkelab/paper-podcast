"""FastAPI 应用入口。

开发态：`uvicorn app.main:app --reload`（前端 Vite 跑在 5173，proxy /api 过来）
生产态：`python -m app.main` 或 uvicorn，若检测到 frontend/dist 存在则一并托管静态文件，
       单端口即可对外服务。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api.routes import router
from .config import PROJECT_ROOT, Settings, get_settings
from .db import Database
from .worker import TaskQueue

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    settings.ensure_dirs()

    logger.info("=" * 62)
    logger.info("  %s v%s", settings.app_name, settings.version)
    llm_label = {
        "deepseek": "DeepSeek API",
        "doubao": "豆包方舟 API",
    }.get(settings.llm_mode, "Mock（未配置 DEEPSEEK_API_KEY / ARK_API_KEY）")
    logger.info("  文本解读：%s", llm_label)
    logger.info("  音频合成：%s", "豆包语音播客" if settings.tts_mode == "doubao" else "Mock（未配置豆包语音密钥）")
    logger.info("  数据库：%s", settings.db_path)
    logger.info("=" * 62)

    app.state.queue = TaskQueue(settings, app.state.db)
    await app.state.queue.start()
    try:
        yield
    finally:
        await app.state.queue.stop()
        app.state.db.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        description="论文解读 AI 播客 —— 上传论文，自动生成双人对谈播客音频",
        lifespan=lifespan,
    )

    app.state.settings = settings
    app.state.db = Database(settings.db_path)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
    )

    app.include_router(router)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request, exc):  # noqa: ANN001
        logger.exception("未处理异常：%s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": f"服务端内部错误：{exc}"})

    _mount_frontend(app)
    return app


class SPAStaticFiles(StaticFiles):
    """托管前端构建产物，并对前端路由做 history 模式回退。

    Vue Router 在真实后端模式下用的是 createWebHistory（无 # 的干净 URL），
    所以用户在 /episode/abc 上按刷新时，请求会打到服务器。裸 StaticFiles 会
    返回 404 —— 表现为「刷新页面就白屏」。这里把找不到的路径回退到 index.html，
    交给前端路由处理。

    但 /api/* 的 404 必须原样返回，不能回退成 HTML —— 否则前端拿到的会是
    一坨 HTML 而不是 JSON 错误，调试时会非常困惑。
    """

    async def get_response(self, path: str, scope):  # noqa: ANN001
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and not path.startswith("api/"):
                return await super().get_response("index.html", scope)
            raise


def _mount_frontend(app: FastAPI) -> None:
    """如果前端已经构建过，就直接托管，实现单端口部署。"""
    if not FRONTEND_DIST.exists():
        logger.info("未发现前端构建产物（%s），仅提供 API", FRONTEND_DIST)
        return
    app.mount("/", SPAStaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
    logger.info("已托管前端静态文件：%s", FRONTEND_DIST)


app = create_app()


if __name__ == "__main__":
    import uvicorn

    _settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=_settings.host,
        port=_settings.port,
        reload=False,
    )
