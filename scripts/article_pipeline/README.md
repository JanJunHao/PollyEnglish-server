# 图文采集管线（Q4 图文线 · 阶段 1）

采集真实英文文本 → 评级 → 切段 → LLM 标注 → 入 Polly 图文内容库。

来源（按优先级，均为合规授权）：

- **Simple English Wikipedia**（CC BY-SA）— `fetch_simple_wikipedia.py`，天然简化英语；
- **Wikinews**（CC BY）— `fetch_wikinews.py`，时事新闻，更新快；
- **Project Gutenberg**（公有领域）— `fetch_gutenberg.py`，经典文学节选，适合高难度档。

三个连接器输出**同一套 article fetch JSON**，被 `article_ingest.py` 无差别消费。

## 设计概览

```
[条目标题 / 随机 / 最新 / 书 id]
        ↓ fetch_simple_wikipedia.py / fetch_wikinews.py / fetch_gutenberg.py（确定性连接器）
[article fetch JSON: 标题 + 正文 + 配图 + 署名（CC BY-SA / CC BY / 公有领域）]
        ↓ article_ingest.py
        ├─ cefr_grader.py 评 CEFR 难度（复用 cc_pipeline）
        ├─ segment.py 切段（自然段 → 句子级 segment）
        ├─ 配图下载转存到 storage
        ├─ classify_topics.py 受控题材分类 → Content.topics（落 app/taxonomy.py 受控词表）
        ├─ segment 逐句翻译 → paragraphs[].translation  ← 复用 translation_pipeline
        ├─ LLM 标注：逐句讲解（Explanation）+ 查词卡（Word）  ← 复用 app/api/ai.py
        └─ 写库：contents(kind='article') + ArticleDetails
[图文入库，精读层 Word/Explanation 缓存就绪]
        ↓
GET /v1/articles/latest  /  GET /v1/articles/{id}
```

## 复用了哪些现有能力（不重写）

- CEFR 评级：`scripts/cc_pipeline/cefr_grader.py` 的 `CEFRGrader`
- 句子讲解 / 查词卡：`app/api/ai.py` 的 `EXPLAIN_SYSTEM` / `WORD_SYSTEM` 与同款 OpenAI 调用，
  写入与视频共用的 `Explanation` / `Word` 表（精读层形态无关）
- 存储抽象：`app/storage.py` 的 `get_storage()`（配图转存）

## 端到端示例

```bash
cd polly-server

# 1. 采集（任选一个来源；三者输出同一套 fetch JSON）
.venv/bin/python -m scripts.article_pipeline.fetch_simple_wikipedia \
    --titles "Photosynthesis" "Solar System" \
    --out cdn-staging/article-fetch/simplewiki.json
.venv/bin/python -m scripts.article_pipeline.fetch_wikinews \
    --latest 3 --out cdn-staging/article-fetch/wikinews.json
.venv/bin/python -m scripts.article_pipeline.fetch_gutenberg \
    --ids 1342 11 --out cdn-staging/article-fetch/gutenberg.json

# 2. 走完整管线入库（评级 + 切段 + 题材分类 + 翻译 + LLM 标注 + 写库）
.venv/bin/python -m scripts.article_pipeline.article_ingest \
    --from-fetch cdn-staging/article-fetch/simplewiki.json

# 调试：跳过 LLM 标注（不烧 token），只采集 + 评级 + 切段 + 入库
.venv/bin/python -m scripts.article_pipeline.article_ingest \
    --from-fetch cdn-staging/article-fetch/simplewiki.json --no-annotate
```

## 题材分类置信度护栏

`article_ingest.py` 接入了题材分类置信度护栏（类比视频侧 `ingest.py` 的
`classify_confidence < 0.7 → review_pending`）：

- `classify_topics.py` 的 LLM 分类输出带 `confidence`；
- 置信度 < `_CLASSIFY_CONFIDENCE_THRESHOLD`（0.7），或分类返回空 topics（无法归类 /
  API 降级）→ 图文 `status` 落 `review_pending`，**不上首页、不删数据**，等人工复审；
- `classify_confidence` 写入 `Content` 同名列；`--no-annotate` 模式不分类、保持 `published`。

## 数据落点

| 数据 | 落点 |
|---|---|
| 共享元数据（title/cefr_level/attribution/kind='article'/topics） | `contents` 基表 |
| 正文 / 切段（含中英对照 translation）/ 配图 / 字数 / 阅读时长 | `article_details` 明细表 |
| 逐句讲解 | `explanations` 表（`video_id` = article_id） |
| 查词卡 | `words` 表（按 lemma 全局共用，已存在的词跳过） |

## 合规：来源署名

每个连接器为每篇文章生成 attribution 字符串（含标题、来源站点、原文 URL、许可名），
由 `article_ingest.py` 写入 `Content.attribution` 列，`GET /v1/articles/*` 对外返回——
确保署名随内容流转。各来源许可：

- Simple English Wikipedia — CC BY-SA 4.0；
- Wikinews — CC BY 2.5（只需署名，不强制同源）；
- Project Gutenberg — 公有领域（连接器剥掉 Gutenberg 协议头尾，只取 PD 正文）。

## 遗留 TODO

- 受控题材 `topics`：已由 `classify_topics.py` LLM 分类 + `normalize_topics()` 过滤接入，
  并加了置信度护栏；`--no-annotate` 模式下跳过（与翻译 / 标注一致，均依赖 OpenAI）。
- 切段 `translation`：已由 `translation_pipeline.translate_segments()` 逐句翻译接入；
  翻译失败 best-effort 降级（留空，不阻塞入库），与字幕流水线一致。
- 正文清洗：`_clean_extract` 已强化（去表格 / 列表碎片、截断尾部章节）；极端复杂条目
  仍可能残留个别杂质。
- 句子切分：`segment.py` 已加缩写白名单（`Mr.` `Dr.` 月份 / 人名首字母等不误切）；
  白名单偏保守，未收录的罕见缩写后可能漏切（宁可漏切不误切）。
- VOA Learning English 文章连接器：第 8 项可选项，**未做**——VOA 文章站点结构与
  视频侧 `fetch_voa_learning_english.py` 不同，需另写 HTML 解析，留待后续。
- Gutenberg 节选：当前从全文约 1/5 处截 ~600 词的连续整段；尚未做「按章节边界对齐」
  或「同一本书多段节选」，后续可增强。
