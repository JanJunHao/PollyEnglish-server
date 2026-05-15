# CC 内容采集流水线

从 YouTube 发现合规可商用的 CC 视频，自动分级，入 Polly 内容库。

## 设计概览

```
[频道清单 / 关键词]
        ↓ YouTube Data API（搜索 / 列频道）
[候选 videoId 列表]
        ↓ videos.list 拉详情 + 过滤 license=creativeCommon
[CC 候选]
        ↓ yt-dlp 二次验证（API 偶尔会标错）
[license 双重确认]
        ↓ 拉 vtt 字幕（manual 优先 / auto 兜底）
        ↓ yt-dlp 拉 mp4 到 cdn-staging/videos/ （默认开，--no-download-video 跳过）
[字幕 + 全文 + 本地视频文件]
        ↓ CEFR 5 维度评分（词汇 0.4 + Flesch + FK + 句长 + 难词）
[manifest.json: 视频元信息 + 字幕路径 + video_path + CEFR + WPM]
        ↓ to_polly_fetch.py 过滤（默认要 manual 字幕、word_count ≥ 500）
        ↓ video_path 有 → play_mode=native + video_url=<本地 URL>
        ↓ video_path 缺   → play_mode=youtube_embed（向后兼容）
[Polly fetch JSON]
        ↓ scripts/ingest.py --from-fetch
[contents 表（subtitle_url 必填 / cefr_level 喂自 CEFR 评分 / play_mode + video_url）]
```

## 为什么要本地化视频（不直接走 YouTube embed）

YouTube iframe player 在中国大陆访问 `googlevideo.com` 视频流被 GFW 阻断（错误码 152）。
也就是说不带代理就放不了视频。所以服务端用 yt-dlp 预下载 mp4 到 `cdn-staging/videos/`，iOS 端走
AVPlayer 拉局域网 / CDN 上的本地 mp4——既绕过 GFW，也避开 App Store 审核对外部嵌入 YouTube 的红线。

## 安装

```bash
cd polly-server
pip install -e ".[cc-pipeline]"
```

## 频道清单

参见 [cc_channels_30.md](cc_channels_30.md)。三个梯队：

1. **公有领域**：VOA Learning English / NASA / The White House / Library of Congress / NOAA / C-SPAN —— 零法律风险
2. **CC BY**：CrashCourse (BY-SA 注意衍生) / TEDx (混合，逐个验证) / Easy English / Wikimedia
3. **国际机构**：ESA / CERN / World Bank / UN（部分 NC，要看）

⚠️ **禁区**：BBC（含 BBC Learning English）/ TED 主频道 / CNN / Netflix —— 商业再分发即侵权。

## 端到端示例

```bash
# 1. 设置 YouTube API key
export YOUTUBE_API_KEY="..."

# 2. 抓 VOA Learning English 整频道（最值钱的源，自带 Level 分级）
#    默认顺手下载 480p mp4 到 cdn-staging/videos/。带 --no-download-video 可跳过。
python -m scripts.cc_pipeline.yt_cc_scraper \
    --channel UCKyTokYo0nK2OA-az-sDijA \
    --max-results 200 \
    --output ./cdn-staging/cc-content/voa

# 3. 转成 ingest.py 能吃的 fetch JSON
#    默认过滤：license_verified=True + has_subtitle=True + subtitle_source=manual
#    manifest 里 video_path 有的条目，自动输出 play_mode=native + video_url
python -m scripts.cc_pipeline.to_polly_fetch \
    ./cdn-staging/cc-content/voa/manifest.json \
    --out ./cdn-staging/voa-fetch.json \
    --default-category daily_news

# 4. 入库（subtitle_url 失败的自动 review_pending，不进首页）
python -m scripts.ingest \
    --from-fetch ./cdn-staging/voa-fetch.json \
    --limit 100
```

### 已有 manifest 但视频还没下：回填

如果你之前跑 scraper 时带了 `--no-download-video`、或者老 manifest 里没 `video_path` 字段，
单独跑下载脚本回填即可（幂等，DB 已 native + 文件已在的会自动跳过）：

```bash
python -m scripts.cc_pipeline.download_videos \
    --manifest ./cdn-staging/cc-content/voa/manifest.json \
    --quality 480
```

完成后 contents 表对应行的 `play_mode` 已直接切到 `native`、`video_url` 指向本地。

## 单独跑 CEFR 评分

不需要 YouTube API key：

```bash
python -m scripts.cc_pipeline.cefr_grader   # 跑 A1/B1/C1 三个内置 demo
```

输出每段文本的：综合分数 / CEFR 等级 / 5 个子维度分数 / 词汇分布。

## API 配额

YouTube Data API 默认每天 10000 quota：

- `search.list`：100 quota / 次 → 每天最多 100 次搜索
- `playlistItems.list`：1 quota / 次 → 大批量首选「拉整个频道」而非「按关键词搜」
- `videos.list`：1 quota / 50 条 → 拉详情几乎免费

**实操建议**：先吃透 [cc_channels_30.md](cc_channels_30.md) 的频道清单，按频道批量采，留搜索给「补充长尾」。

## 集成进 Polly 现有流水线的字段桥接

| yt_cc_scraper 字段 | Polly contents 表字段 | 备注 |
|---|---|---|
| `youtube_id` | `id` / `youtube_id` | id 用 video id 本身 |
| `channel_title` | `author` / `source` | 双填 |
| `cefr_estimate` | `cefr_level` | 评分器输出直接写入 |
| `has_english_subtitle` | filter | False 直接淘汰 |
| `subtitle_source == 'manual'` | filter | auto 字幕默认淘汰，加 `--allow-auto` 放开 |
| `attribution` | 暂时丢弃 | TODO：写到 contents.attribution 列 |
| `wpm` | 暂时丢弃 | TODO：plan 文档 04.15 「听力难度」二维筛选用 |

## 字幕质量红线：manual-only

两层硬性卡：

**Layer 1 — CC scraper 输出层**（[to_polly_fetch.py](to_polly_fetch.py)）：
默认只放行 `subtitle_source == 'manual'`，过滤掉 auto-caption。auto 字幕错字多、专有名词更是灾难，做精读教材不合格。
要放开（极端缺源场景）：`--allow-auto`。

**Layer 2 — 运行时字幕流水线**（[app/services/subtitle_pipeline.py](../../app/services/subtitle_pipeline.py)）：
yt-dlp tier 1 先试 `--write-sub`（manual），失败才回退 `--write-auto-sub`（auto）。输出的 `subtitles/{job_id}.json` 里带 `subtitle_source` 字段，记录到底用了哪种。
要完全拒绝 auto（连用户从 App 内 YouTube 导入也禁止），改 `ALLOW_AUTO_SUBTITLE = False`。

这两层独立 — Layer 1 在「批量入库 CC 内容」时生效，Layer 2 在「用户主动导入 YouTube URL / 服务端运行时拉字幕」时生效。

## 生产前要做的改造

1. **CEFR 词表换成正版**：[cefr_grader.py](cefr_grader.py) 内置词表只是 fallback；生产环境 `load_cefr_vocab()` 加载 Oxford 3000/5000 或 EVP
2. **attribution 写入 DB**：现有 contents 表没 attribution 列，得加 migration
3. **wpm 写入 DB**：同上，建议起名 `speech_wpm`
4. **lemmatization**：[cefr_grader.py:_tokenize](cefr_grader.py#L149) 换成 spaCy，把 "running" 算到 "run"
5. **频道白名单缓存**：稳定后开 `--no-verify` 提速（跳过 yt-dlp 二次验证）
