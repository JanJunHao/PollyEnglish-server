"""Schema 启动检查。脚本/服务起步时调一下，确保 alembic 已经跑到 head。

不在 main.py 自动跑 migration（生产部署可能由 CI/CD 单独跑）；
而是 fail-fast：发现 alembic_version 落后于代码就报错，让人工 `alembic upgrade head`。
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from app.config import get_settings
from app.db import engine

log = logging.getLogger("polly.bootstrap")

_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _expected_head() -> str | None:
    cfg = Config(str(_ALEMBIC_INI))
    return ScriptDirectory.from_config(cfg).get_current_head()


def _current_version() -> str | None:
    inspector = inspect(engine)
    if "alembic_version" not in inspector.get_table_names():
        return None
    with engine.connect() as conn:
        row = conn.exec_driver_sql("SELECT version_num FROM alembic_version").fetchone()
        return row[0] if row else None


def assert_schema_up_to_date() -> None:
    """schema 落后于代码时抛错。

    Usage:
        from app.db_bootstrap import assert_schema_up_to_date
        assert_schema_up_to_date()
    """
    head = _expected_head()
    current = _current_version()
    if head is None:
        return  # 没 migration 历史，跳过
    if current is None:
        raise RuntimeError(
            "数据库没有 alembic_version 表。先跑：alembic upgrade head\n"
            "（如果旧库已经有所有表，跑：alembic stamp head）"
        )
    if current != head:
        raise RuntimeError(
            f"数据库 schema 落后：当前 {current}，期望 {head}。先跑：alembic upgrade head"
        )


def ensure_schema_dev() -> None:
    """开发期便捷：自动跑 alembic upgrade head 到最新版本。
    生产不要用——生产 schema 升级应该走 CI/CD pipeline。
    """
    if get_settings().database_url.startswith("postgresql"):
        log.warning("ensure_schema_dev() 不应在 PostgreSQL 上调用，请走 alembic upgrade head")
        return
    from alembic import command as alembic_command
    cfg = Config(str(_ALEMBIC_INI))
    alembic_command.upgrade(cfg, "head")
