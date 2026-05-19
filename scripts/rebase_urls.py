"""把 contents 表里自托管资源 URL 的 scheme+host 换成当前对外地址。

场景：PUBLIC_BASE_URL 变了（局域网 IP 变动 / 切到 Cloudflare 隧道 / 隧道 URL 重启后变化），
库里历史的 video_url / subtitle_url / thumbnail_url 仍指向旧地址，App 端就拉不到资源。
本脚本只改 path 含 /static/ 的自托管 URL，外部 URL（如 bundle://、第三方源）保持不动。

用法：
    python -m scripts.rebase_urls --dry-run         # 目标地址取自 .env 的 PUBLIC_BASE_URL
    python -m scripts.rebase_urls
    python -m scripts.rebase_urls --to https://xxx.trycloudflare.com
"""

from __future__ import annotations

import argparse
import sys
from urllib.parse import urlparse

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Content

_URL_FIELDS = ("video_url", "subtitle_url", "thumbnail_url")


def _rebase(url: str | None, new_base: str) -> str | None:
    """只改自托管资源（path 含 /static/）的 scheme+host，其余原样返回。"""
    if not url:
        return url
    parsed = urlparse(url)
    if "/static/" not in parsed.path:
        return url
    nb = urlparse(new_base)
    rebased = f"{nb.scheme}://{nb.netloc}{parsed.path}"
    return f"{rebased}?{parsed.query}" if parsed.query else rebased


def main() -> None:
    parser = argparse.ArgumentParser(description="rebase contents 自托管资源 URL 到当前对外地址")
    parser.add_argument("--to", help="目标 base URL，默认取 .env 的 PUBLIC_BASE_URL")
    parser.add_argument("--dry-run", action="store_true", help="只打印将变更的 URL，不写库")
    args = parser.parse_args()

    new_base = (args.to or get_settings().base_url()).rstrip("/")
    print(f"目标 base: {new_base}", file=sys.stderr)

    changed = 0
    with SessionLocal() as db:
        rows = list(db.scalars(select(Content)))
        for r in rows:
            for field in _URL_FIELDS:
                old = getattr(r, field)
                new = _rebase(old, new_base)
                if new == old:
                    continue
                changed += 1
                print(f"{r.id}.{field}: {old} -> {new}")
                if not args.dry_run:
                    setattr(r, field, new)
        if not args.dry_run:
            db.commit()

    tag = "[dry-run] " if args.dry_run else ""
    print(f"\n--- {tag}汇总 ---  共 {len(rows)} 条，{changed} 个 URL "
          f"{'待变更' if args.dry_run else '已变更'}", file=sys.stderr)


if __name__ == "__main__":
    main()
