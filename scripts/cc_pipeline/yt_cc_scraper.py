#!/usr/bin/env python3
"""
YouTube Creative Commons Content Scraper
=========================================

发现、验证、处理 YouTube 上 CC 协议视频，用于教育内容库构建。

核心能力：
  1. 通过关键词搜索 CC 视频 (videoLicense=creativeCommon)
  2. 拉取整个频道的全部视频，自动过滤非 CC
  3. 用 yt-dlp 二次验证 License（API 偶尔会误报）
  4. 下载英文字幕（优先 manual，回退到 auto）
  5. 调用 cefr_grader 自动评估难度，输出元数据 JSON

依赖：
    pip install google-api-python-client yt-dlp textstat

用法：
    export YOUTUBE_API_KEY="your_key_here"
    # 按关键词搜索
    python yt_cc_scraper.py --query "english learning" --max-results 50
    # 抓取整个频道（例如 VOA Learning English）
    python yt_cc_scraper.py --channel UCV1h_cBE0Drrx8Q9pODBNYg --max-results 200
"""

import os
import re
import json
import time
import logging
import argparse
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any, Tuple

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import yt_dlp


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)


class ScraperError(RuntimeError):
    """发现层的硬错误（频道不存在、API key 无效、配额耗尽等）。
    与「频道为空」区分开：硬错误必须让 main() 返回非 0，否则会和「成功但 0 结果」混淆。"""


@dataclass
class VideoMetadata:
    """单个 CC 视频的完整元数据。"""
    youtube_id: str
    title: str
    description: str
    channel_id: str
    channel_title: str
    published_at: str
    duration_seconds: int
    view_count: int
    like_count: int
    license: str
    license_verified: bool
    has_english_subtitle: bool
    subtitle_source: str            # 'manual' / 'auto' / 'none'
    word_count: int
    wpm: Optional[float]            # words per minute
    cefr_estimate: Optional[str]    # A1 / A2 / B1 / B2 / C1 / C2
    composite_score: Optional[float]
    fetched_at: str
    attribution: str                # 法律要求的署名文本
    video_path: Optional[str] = None  # 相对 cdn-staging 的本地视频路径，例如 "videos/{id}.mp4"


class YouTubeCCScraper:
    """从 YouTube 发现、验证、处理 CC 视频的主类。"""

    def __init__(self, api_key: str, output_dir: str = './cc_content'):
        self.youtube = build('youtube', 'v3', developerKey=api_key)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.subtitle_dir = self.output_dir / 'subtitles'
        self.subtitle_dir.mkdir(exist_ok=True)
        self.metadata_dir = self.output_dir / 'metadata'
        self.metadata_dir.mkdir(exist_ok=True)
        self.manifest_path = self.output_dir / 'manifest.json'

    # ----------------- 发现层：搜索 / 频道列表 -----------------

    def search_cc_videos(
        self,
        query: str,
        max_results: int = 50,
        published_after: Optional[str] = None,
        order: str = 'viewCount',
    ) -> List[str]:
        """按关键词搜索 CC 视频。

        注意：每次 search.list 消耗 100 quota（默认每天 10000）。
        """
        video_ids: List[str] = []
        next_page_token: Optional[str] = None

        while len(video_ids) < max_results:
            params = {
                'part': 'id',
                'q': query,
                'type': 'video',
                'videoLicense': 'creativeCommon',
                'maxResults': min(50, max_results - len(video_ids)),
                'order': order,
            }
            if next_page_token:
                params['pageToken'] = next_page_token
            if published_after:
                params['publishedAfter'] = published_after

            try:
                resp = self.youtube.search().list(**params).execute()
            except HttpError as e:
                # 第一页失败多半是认证/配额硬错，必须冒上去；后续页失败只截断结果
                if not video_ids:
                    raise ScraperError(f"Search API error on first page: {e}") from e
                logger.error(f"Search API error on subsequent page: {e}")
                break

            for item in resp.get('items', []):
                video_ids.append(item['id']['videoId'])
                if len(video_ids) >= max_results:
                    break

            next_page_token = resp.get('nextPageToken')
            if not next_page_token:
                break
            time.sleep(0.5)

        video_ids = video_ids[:max_results]
        logger.info(f"Search '{query}' -> {len(video_ids)} CC candidates")
        return video_ids

    def list_channel_videos(
        self,
        channel_id: str,
        max_results: int = 1000,
    ) -> List[str]:
        """列出某个频道的全部视频 ID（后续按 license 过滤）。

        每次 playlistItems.list 仅消耗 1 quota，是大批量采集的首选方式。
        """
        # 1. 解析频道的 uploads playlist
        try:
            ch_resp = self.youtube.channels().list(
                part='contentDetails',
                id=channel_id
            ).execute()
        except HttpError as e:
            raise ScraperError(f"Cannot resolve channel {channel_id}: {e}") from e
        items = ch_resp.get('items', [])
        if not items:
            raise ScraperError(f"Channel not found: {channel_id}")
        uploads = items[0]['contentDetails']['relatedPlaylists']['uploads']

        # 2. 翻页拉取
        video_ids: List[str] = []
        next_page_token: Optional[str] = None
        while len(video_ids) < max_results:
            try:
                pl_resp = self.youtube.playlistItems().list(
                    part='contentDetails',
                    playlistId=uploads,
                    maxResults=50,
                    pageToken=next_page_token,
                ).execute()
                for item in pl_resp.get('items', []):
                    video_ids.append(item['contentDetails']['videoId'])
                    if len(video_ids) >= max_results:
                        break
                next_page_token = pl_resp.get('nextPageToken')
                if not next_page_token:
                    break
                time.sleep(0.3)
            except HttpError as e:
                logger.error(f"playlistItems error: {e}")
                break

        video_ids = video_ids[:max_results]
        logger.info(f"Channel {channel_id} -> {len(video_ids)} videos (pre-filter)")
        return video_ids

    # ----------------- 验证层：License + 元数据 -----------------

    def get_video_details(self, video_ids: List[str]) -> List[Dict[str, Any]]:
        """批量获取视频详情，只保留 license=creativeCommon 的。"""
        kept: List[Dict[str, Any]] = []
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i:i + 50]
            try:
                resp = self.youtube.videos().list(
                    part='snippet,contentDetails,statistics,status',
                    id=','.join(batch),
                ).execute()
                for item in resp.get('items', []):
                    if item.get('status', {}).get('license') == 'creativeCommon':
                        kept.append(item)
                time.sleep(0.3)
            except HttpError as e:
                logger.error(f"videos.list error: {e}")
        logger.info(f"License filter: {len(kept)} / {len(video_ids)} are CC")
        return kept

    def verify_license_with_ytdlp(self, video_id: str) -> bool:
        """二次验证：YouTube API 偶尔会把 license 标错，用 yt-dlp 抓页面再确认。"""
        try:
            opts = {'quiet': True, 'no_warnings': True, 'skip_download': True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(
                    f"https://www.youtube.com/watch?v={video_id}",
                    download=False,
                )
            license_str = (info.get('license') or '').lower()
            return 'creative commons' in license_str
        except Exception as e:
            logger.warning(f"yt-dlp verification failed for {video_id}: {e}")
            return False

    # ----------------- 字幕层 -----------------

    def download_subtitles(self, video_id: str) -> Dict[str, Any]:
        """下载英文字幕。优先 manual（作者上传），回退到 auto-generated。"""
        result = {
            'has_subtitle': False,
            'source': 'none',
            'text': '',
            'word_count': 0,
            'duration_seconds': 0,
        }

        opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['en', 'en-US', 'en-GB'],
            'subtitlesformat': 'vtt',
            'outtmpl': str(self.subtitle_dir / f"{video_id}.%(ext)s"),
            # YouTube 反爬：缺 JS 挑战求解器时视频流不可用，但字幕走独立 endpoint。
            # 不加这条就会因「无可用视频格式」抛 ExtractorError，连字幕都拿不到。
            'ignore_no_formats_error': True,
        }

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        except Exception as e:
            logger.warning(f"Subtitle download failed for {video_id}: {e}")
            return result

        # 优先找 manual 字幕（文件名形如 video_id.en.vtt 而不是 video_id.en-auto.vtt）
        candidates = sorted(self.subtitle_dir.glob(f"{video_id}*.vtt"))
        if not candidates:
            return result

        # 排序：manual 优先于 auto
        manual = [p for p in candidates if 'auto' not in p.name.lower()]
        chosen = manual[0] if manual else candidates[0]
        source = 'manual' if chosen in manual else 'auto'

        text, duration = self._parse_vtt(chosen)
        result.update({
            'has_subtitle': True,
            'source': source,
            'text': text,
            'word_count': len(text.split()),
            'duration_seconds': duration,
        })
        return result

    @staticmethod
    def _parse_vtt(vtt_path: Path) -> Tuple[str, float]:
        """从 VTT 文件提取纯文本和最后一条字幕的结束时间戳（秒）。"""
        text_lines: List[str] = []
        last_ts = 0.0

        with open(vtt_path, 'r', encoding='utf-8') as f:
            content = f.read()

        for block in content.split('\n\n'):
            lines = block.strip().split('\n')
            if len(lines) < 2:
                continue
            for i, line in enumerate(lines):
                if '-->' in line:
                    # 形如 "00:01:23.456 --> 00:01:25.789 ..."
                    end_str = line.split('-->')[1].strip().split(' ')[0]
                    parts = end_str.split(':')
                    try:
                        if len(parts) == 3:
                            h, m, s = parts
                            last_ts = int(h) * 3600 + int(m) * 60 + float(s)
                        elif len(parts) == 2:
                            m, s = parts
                            last_ts = int(m) * 60 + float(s)
                    except ValueError:
                        pass
                    text_lines.extend(lines[i + 1:])
                    break

        full = ' '.join(text_lines)
        full = re.sub(r'<[^>]+>', '', full)        # 去 <c>...</c> 等标签
        full = re.sub(r'&[a-z]+;', ' ', full)      # 去 HTML entity
        full = re.sub(r'\s+', ' ', full).strip()
        return full, last_ts

    # ----------------- 工具方法 -----------------

    @staticmethod
    def parse_iso_duration(s: str) -> int:
        """ISO 8601 PT5M30S → 330 秒。"""
        m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', s)
        if not m:
            return 0
        h, mn, sec = m.groups(default='0')
        return int(h) * 3600 + int(mn) * 60 + int(sec)

    def _build_attribution(self, video: Dict[str, Any]) -> str:
        """生成 CC BY 协议要求的署名文本。"""
        return (
            f'"{video["snippet"]["title"]}" by '
            f'{video["snippet"]["channelTitle"]}, '
            f'licensed under CC BY 3.0. '
            f'Source: https://www.youtube.com/watch?v={video["id"]}'
        )

    # ----------------- 主流程 -----------------

    def process_videos(
        self,
        video_ids: List[str],
        verify_with_ytdlp: bool = True,
        grade_difficulty: bool = True,
        download_video: bool = True,
        video_quality: int = 480,
        target: Optional[int] = None,
    ) -> List[VideoMetadata]:
        """完整流水线：详情 → license 验证 → 字幕 → 难度评估 → (可选)视频下载 → 入库。

        download_video=True 时把 mp4 拉到 polly-server/cdn-staging/videos/{id}.mp4，
        VideoMetadata.video_path 记录相对路径，给下游 to_polly_fetch 转 play_mode=native 用。

        target：收集到这么多条「带 manual 字幕」的视频就提前结束。用于「频道很深、
        只有老视频是 CC」的场景——避免给几百条候选逐个白跑字幕下载。
        """
        grader = None
        if grade_difficulty:
            try:
                # 同包导入；脚本以 `python -m scripts.cc_pipeline.yt_cc_scraper` 跑起
                from .cefr_grader import CEFRGrader
                grader = CEFRGrader()
            except ImportError as e:
                logger.warning("cefr_grader not found: %s; skipping difficulty grading", e)

        details = self.get_video_details(video_ids)
        results: List[VideoMetadata] = []

        for video in details:
            vid = video['id']
            title = video['snippet']['title']
            logger.info(f"Processing {vid}: {title[:60]}")

            verified = True
            if verify_with_ytdlp:
                verified = self.verify_license_with_ytdlp(vid)
                if not verified:
                    logger.warning(f"License mismatch for {vid}, skipping")
                    continue

            sub_data = self.download_subtitles(vid)

            video_path: Optional[str] = None
            if download_video:
                # 全局 videos/ 目录而非 per-channel，对齐 polly-server/cdn-staging/videos/ 的 /static 挂载
                from .download_videos import download_video as _dl
                videos_root = Path(__file__).resolve().parent.parent.parent / "cdn-staging" / "videos"
                videos_root.mkdir(parents=True, exist_ok=True)
                local = _dl(vid, videos_root, quality=video_quality)
                if local:
                    video_path = f"videos/{vid}.mp4"
                else:
                    logger.warning(f"{vid}: 视频下载失败（manifest video_path 留空，下游将走 youtube_embed）")

            wpm = None
            cefr = None
            composite = None
            if grader and sub_data['has_subtitle'] and sub_data['duration_seconds'] > 0:
                duration_min = sub_data['duration_seconds'] / 60
                if duration_min > 0:
                    wpm = sub_data['word_count'] / duration_min
                grading = grader.grade(sub_data['text'])
                cefr = grading.get('cefr')
                composite = grading.get('composite_score')

            meta = VideoMetadata(
                youtube_id=vid,
                title=title,
                description=video['snippet'].get('description', '')[:500],
                channel_id=video['snippet']['channelId'],
                channel_title=video['snippet']['channelTitle'],
                published_at=video['snippet']['publishedAt'],
                duration_seconds=self.parse_iso_duration(
                    video['contentDetails']['duration']
                ),
                view_count=int(video['statistics'].get('viewCount', 0)),
                like_count=int(video['statistics'].get('likeCount', 0)),
                license='creativeCommon',
                license_verified=verified,
                has_english_subtitle=sub_data['has_subtitle'],
                subtitle_source=sub_data['source'],
                word_count=sub_data['word_count'],
                wpm=round(wpm, 1) if wpm else None,
                cefr_estimate=cefr,
                composite_score=composite,
                fetched_at=datetime.utcnow().isoformat() + 'Z',
                attribution=self._build_attribution(video),
                video_path=video_path,
            )

            self._save_metadata(meta)
            results.append(meta)
            time.sleep(0.3)

            if target is not None:
                manual_count = sum(1 for r in results if r.subtitle_source == "manual")
                if manual_count >= target:
                    logger.info(f"已收集 {manual_count} 条 manual 字幕视频，达到 target，提前结束")
                    break

        self._save_manifest(results)
        return results

    def _save_metadata(self, meta: VideoMetadata) -> None:
        path = self.metadata_dir / f"{meta.youtube_id}.json"
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(asdict(meta), f, ensure_ascii=False, indent=2)

    def _save_manifest(self, results: List[VideoMetadata]) -> None:
        """汇总所有视频到一个 manifest，方便后续筛选。"""
        manifest = {
            'generated_at': datetime.utcnow().isoformat() + 'Z',
            'total': len(results),
            'videos': [asdict(m) for m in results],
        }
        with open(self.manifest_path, 'w', encoding='utf-8') as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        logger.info(f"Manifest saved: {self.manifest_path}")


def main():
    parser = argparse.ArgumentParser(description="YouTube CC content scraper")
    parser.add_argument('--query', help='搜索关键词')
    parser.add_argument('--channel', help='YouTube channel ID')
    parser.add_argument('--max-results', type=int, default=50)
    parser.add_argument('--output', default='./cc_content')
    parser.add_argument('--no-verify', action='store_true',
                        help='跳过 yt-dlp 二次验证（更快但可能采到误标）')
    parser.add_argument('--no-grade', action='store_true',
                        help='跳过 CEFR 难度评估')
    parser.add_argument('--no-download-video', action='store_true',
                        help='跳过视频下载（manifest 只含字幕/元数据，下游走 youtube_embed）')
    parser.add_argument('--video-quality', type=int, default=480,
                        help='视频最高高度像素，默认 480')
    parser.add_argument('--target', type=int,
                        help='收集到这么多条 manual 字幕视频就提前结束（适合深翻老频道）')
    parser.add_argument('--published-after', help='ISO 8601 起始时间')
    args = parser.parse_args()

    api_key = os.environ.get('YOUTUBE_API_KEY')
    if not api_key:
        logger.error("请先设置环境变量 YOUTUBE_API_KEY")
        return 1

    if not args.query and not args.channel:
        logger.error("必须指定 --query 或 --channel")
        return 1

    scraper = YouTubeCCScraper(api_key, output_dir=args.output)

    try:
        if args.query:
            ids = scraper.search_cc_videos(
                args.query,
                max_results=args.max_results,
                published_after=args.published_after,
            )
        else:
            ids = scraper.list_channel_videos(args.channel, max_results=args.max_results)
    except ScraperError as e:
        logger.error(f"发现层失败：{e}")
        return 1

    results = scraper.process_videos(
        ids,
        verify_with_ytdlp=not args.no_verify,
        grade_difficulty=not args.no_grade,
        download_video=not args.no_download_video,
        video_quality=args.video_quality,
        target=args.target,
    )

    logger.info(f"完成：成功处理 {len(results)} 个 CC 视频")
    logger.info(f"输出目录：{args.output}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
