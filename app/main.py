import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import ai, chat, contents, health, pronunciation, subtitles, translations
from app.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

settings = get_settings()

# Schema 由 Alembic 管理：部署/测试前必须跑 `alembic upgrade head`。
# 之前的 Base.metadata.create_all 已废弃——它会跟 alembic_version 表脱钩，autogenerate 失真。

app = FastAPI(title="polly-server", version="0.1.0")

origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(contents.router)
app.include_router(ai.router)
app.include_router(subtitles.router)
app.include_router(translations.router)
app.include_router(pronunciation.router)
app.include_router(chat.router)

# 本地 CDN staging：ingest 把 Polly/Resources 下的 mp4 / 字幕 / 字典等
# 拷贝到 cdn-staging/，挂到 /static/，让 iOS 模拟器/真机能直接拉。
# 上线时整个 /static/ 路由换成 R2 CDN URL。
_staging = Path(__file__).resolve().parent.parent / "cdn-staging"
_staging.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_staging)), name="static")


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {"name": "polly-server", "docs": "/docs"}
