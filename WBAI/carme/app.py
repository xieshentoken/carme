"""常驻后端，提供统一 API 及 React / PWA 静态产物。"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config as config_module
from .api.routes import WEB_DIR, build_router, require_token
from .bus import EventBus
from .runtime import Runtime
from .store import Store

log = logging.getLogger("carme.app")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-7s %(name)-18s %(message)s",
        datefmt="%H:%M:%S",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    store: Store = app.state.store
    runtime: Runtime = app.state.runtime

    stale = store.mark_stale_running_as_failed()
    if stale:
        log.warning("清理了 %d 条上次未正常结束的任务", stale)

    log.info("Carme 已启动 —— 成员：%s", ", ".join(app.state.config.agents.agents))
    log.info("可用供应商：%s", ", ".join(
        n for n, p in app.state.config.models.providers.items() if p.available
    ) or "（无，请配置 .env 里的 API key）")

    try:
        yield
    finally:
        await runtime.shutdown()
        store.close()
        log.info("Carme 已停止")


def create_app() -> FastAPI:
    setup_logging(os.getenv("CARME_LOG_LEVEL", "INFO"))
    config = config_module.load()

    data_dir = Path(os.getenv("CARME_DATA_DIR", config_module.DATA_DIR))
    store = Store(data_dir / "carme.db")
    bus = EventBus()
    runtime = Runtime(config, store, bus)

    app = FastAPI(
        title="Carme",
        description="自托管的常驻 AI 员工团队",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.store = store
    app.state.bus = bus
    app.state.runtime = runtime

    # 网页与 API 同源部署；开发时可显式配置开发服务器来源。
    origins = [item.strip() for item in os.getenv("CARME_CORS_ORIGINS", "").split(",") if item.strip()]
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False,
                           allow_methods=["*"], allow_headers=["*"])

    app.include_router(build_router(config, store, runtime), dependencies=[Depends(require_token)])

    # ---------------- 前端 ----------------

    web_dir = WEB_DIR / "dist"
    if web_dir.exists():
        if (web_dir / "assets").exists():
            app.mount("/assets", StaticFiles(directory=str(web_dir / "assets")), name="assets")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(str(web_dir / "index.html"), headers={"Cache-Control": "no-cache"})

        @app.get("/manifest.webmanifest", include_in_schema=False)
        async def manifest() -> FileResponse:
            return FileResponse(str(web_dir / "manifest.webmanifest"))

        @app.get("/sw.js", include_in_schema=False)
        async def service_worker() -> FileResponse:
            # Service Worker 必须从根路径提供，作用域才覆盖全站
            return FileResponse(str(web_dir / "sw.js"), media_type="application/javascript",
                                headers={"Cache-Control": "no-cache"})

        @app.get("/{filename}", include_in_schema=False)
        async def public_asset(filename: str):
            allowed = {"favicon.png", "apple-touch-icon.png", "icon-192.png", "icon-512.png", "icon-maskable.png"}
            if filename not in allowed or not (web_dir / filename).is_file():
                return JSONResponse({"detail": "资源不存在"}, status_code=404)
            return FileResponse(str(web_dir / filename))
    else:

        @app.get("/", include_in_schema=False)
        async def index_missing() -> JSONResponse:
            return JSONResponse({"detail": "网页尚未构建，请在 WBAI/web 执行 npm ci 和 npm run build。API 已可用。"}, status_code=503)

    return app


app = create_app()
