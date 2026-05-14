"""把 DemoVideo.swift 里硬编码的 3 个视频写入数据库。
对应 plan「关键文件改动 — 当前 demo Phase A」迁 manifest 的服务端版本。
"""

from __future__ import annotations

from app.db import SessionLocal
from app.db_bootstrap import ensure_schema_dev
from app.models import Content

ensure_schema_dev()


DEMO_CONTENTS: list[dict] = [
    {
        "id": "julian-treasure",
        "title": "How to speak so that people want to listen",
        "author": "Julian Treasure",
        "source": "TED",
        "duration_seconds": 9 * 60 + 58,
        "cefr_level": "B2",
        # TED 合规：上架版必须走 YouTube embed，不 host mp4
        "play_mode": "youtube_embed",
        "video_url": None,
        "youtube_id": "eIho2S0ZahI",
        "thumbnail_url": "bundle://julian-treasure-maxresdefault",
        "subtitle_url": "bundle://subtitles/julian-treasure.json",
        "vocabulary_url": "bundle://vocabulary/julian-treasure.json",
        "explanation_url": "bundle://explanations/julian-treasure.json",
        "categories": ["ted", "highlights"],
        "category_color_hex": 0xFFE066,
        "is_recommended": True,
        "classify_confidence": 0.98,
        "status": "published",
    },
    {
        "id": "ted-ed-dream",
        "title": "Why do we dream?",
        "author": "TED-Ed",
        "source": "TED-Ed",
        "duration_seconds": 4 * 60 + 58,
        "cefr_level": "B1",
        "play_mode": "youtube_embed",
        "video_url": None,
        "youtube_id": "2W85Dwxx218",
        "thumbnail_url": "bundle://ted-ed-dream-maxresdefault",
        "subtitle_url": "bundle://subtitles/ted-ed-dream.json",
        "vocabulary_url": "bundle://vocabulary/ted-ed-dream.json",
        "explanation_url": "bundle://explanations/ted-ed-dream.json",
        "categories": ["discovery", "ted"],
        "category_color_hex": 0xB8C4FF,
        "is_recommended": False,
        "classify_confidence": 0.95,
        "status": "published",
    },
    {
        "id": "tim-urban",
        "title": "Inside the mind of a master procrastinator",
        "author": "Tim Urban",
        "source": "TED",
        "duration_seconds": 14 * 60 + 4,
        "cefr_level": "C1",
        "play_mode": "youtube_embed",
        "video_url": None,
        "youtube_id": "arj7oStGLkU",
        "thumbnail_url": "bundle://tim-urban-maxresdefault",
        "subtitle_url": "bundle://subtitles/tim-urban.json",
        "vocabulary_url": "bundle://vocabulary/tim-urban.json",
        "explanation_url": "bundle://explanations/tim-urban.json",
        "categories": ["ted", "highlights"],
        "category_color_hex": 0xFFAC75,
        "is_recommended": False,
        "classify_confidence": 0.97,
        "status": "published",
    },
]


def main() -> None:
    with SessionLocal() as db:
        for payload in DEMO_CONTENTS:
            existing = db.get(Content, payload["id"])
            if existing:
                for k, v in payload.items():
                    setattr(existing, k, v)
                action = "updated"
            else:
                db.add(Content(**payload))
                action = "inserted"
            print(f"{action}: {payload['id']}")
        db.commit()


if __name__ == "__main__":
    main()
