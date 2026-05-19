from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Content(Base):
    """内容元信息（统一基表）。Q2 统一内容模型：基表存所有形态共享的学习元数据，
    形态特有字段拆到明细表（视频字段暂留基表，图文见 ArticleDetails）。
    schema 兼容 SQLite + PostgreSQL。
    """

    __tablename__ = "contents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(512))
    author: Mapped[str] = mapped_column(String(256))
    source: Mapped[str] = mapped_column(String(64))
    cefr_level: Mapped[str] = mapped_column(String(8))

    # ── Q2 共享元数据 ──
    # 内容形态（modality）：video / article / audio。入库时确定，驱动按形态分流。
    kind: Mapped[str] = mapped_column(
        String(16), default="video", server_default="video", index=True
    )
    # 内容语言（ISO 639-1）。英语学习内容默认 en。
    language: Mapped[str] = mapped_column(String(16), default="en", server_default="en")
    # 受控题材标签（app/taxonomy.py 的 topic id 列表）。与自由 categories 并存，逐步替代。
    topics: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    # Q1 质量打分器产出：L1 音频可教性分 + 综合质量分。未评分为 None。
    audio_teachability: Mapped[float | None] = mapped_column(Float, nullable=True)
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # CC 内容署名（CC BY-SA 等许可要求保留来源与作者）。
    attribution: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # ── 视频形态字段（kind='video' 时有效；article 行为 NULL）──
    # native = AVPlayer 流式播放 CDN mp4；youtube_embed = WKWebView iFrame（TED 合规走这条）
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    play_mode: Mapped[str | None] = mapped_column(String(16), default="native", nullable=True)
    video_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    youtube_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    thumbnail_url: Mapped[str] = mapped_column(String(1024))
    subtitle_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    vocabulary_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    explanation_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    categories: Mapped[list] = mapped_column(JSON, default=list)
    category_color_hex: Mapped[int] = mapped_column(Integer, default=0xFFE066)
    is_recommended: Mapped[bool] = mapped_column(Boolean, default=False)

    # AI 自动打标置信度；< 0.7 时 status 应为 review_pending 不上首页
    classify_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="published", index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, index=True
    )


class ArticleDetails(Base):
    """图文内容的形态明细。与 contents 基表 1:1——content_id 既是主键也是外键。
    Q2 统一内容模型：基表（Content）存共享元数据，本表存图文特有字段。
    contents 行的 kind='article' 时才有对应明细行。
    """

    __tablename__ = "article_details"

    content_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("contents.id", ondelete="CASCADE"), primary_key=True
    )
    # 文章全文（原始正文）
    body: Mapped[str] = mapped_column(Text)
    # 切段后的段落/句子，精读交互用——对应视频的字幕 segments。
    # 结构：[{"id": 0, "text": "...", "translation": "..."}]
    paragraphs: Mapped[list] = mapped_column(JSON, default=list)
    # 配图 URL 列表（自托管 / CC 图源）
    image_urls: Mapped[list] = mapped_column(JSON, default=list)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    # 估算阅读时长（秒）；首页与视频 duration_seconds 对位展示
    reading_time_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Explanation(Base):
    """AI 句子讲解预生成缓存。
    复合主键 (video_id, segment_id)。命中时直接返回，未命中才调 OpenAI。
    用 pregenerate_explanations.py 批量灌库。
    """

    __tablename__ = "explanations"

    video_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("contents.id", ondelete="CASCADE"), primary_key=True
    )
    segment_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sentence: Mapped[str] = mapped_column(String(2048))

    natural_translation: Mapped[str] = mapped_column(String(2048))
    core_explanation: Mapped[str] = mapped_column(String(2048))
    key_vocab: Mapped[list] = mapped_column(JSON, default=list)
    grammar_point: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    cultural_note: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    pronunciation_tip: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    similar_expressions: Mapped[list | None] = mapped_column(JSON, nullable=True)

    model: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Word(Base):
    """AI 查词预生成缓存。
    key = 小写 lemma；同一个 lemma 跨视频共用，不按上下文分桶。
    （上下文消歧未来再做：可改成 (word, sense_hint) 复合主键。）
    """

    __tablename__ = "words"

    word: Mapped[str] = mapped_column(String(64), primary_key=True)
    phonetic: Mapped[str] = mapped_column(String(128), default="")
    level: Mapped[str] = mapped_column(String(8), default="")
    definitions: Mapped[list] = mapped_column(JSON, default=list)
    model: Mapped[str] = mapped_column(String(64))
    hit_count: Mapped[int] = mapped_column(Integer, default=0)  # 用于后期判断哪些词值得人工复核
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ContentFeedback(Base):
    """用户反馈某视频分类不对 / 内容质量差。
    plan「AI 自动化打标流水线 - 全 AI 自动化的护栏」：
    反馈累积到阈值触发 review_pending，把视频从首页摘掉。
    """

    __tablename__ = "content_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("contents.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), index=True)
    # kind 取值：wrong_category / poor_audio / poor_video / wrong_subtitle / other
    note: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # 简易反 spam：同一 client_fp（来自 IP + UA hash）短期内重复反馈合并
    client_fp: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SubtitleJob(Base):
    """字幕生成任务。运行时流水线：用户/ingest 给 YouTube URL，
    worker 拉 vtt（auto-caption 优先 / Whisper 兜底），输出 Polly SubtitleDocument JSON 到 cdn-staging。
    plan「字幕生成流水线（运行时）」。

    status：
    - pending  入队，等 worker 拉起
    - running  worker 处理中（yt-dlp 拉字幕 / Whisper 转录）
    - done     成功，result_subtitle_url 可读
    - failed   失败，error_message 给原因
    """

    __tablename__ = "subtitle_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # UUID4
    source_url: Mapped[str] = mapped_column(String(2048))
    youtube_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    result_subtitle_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    segments_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class SubtitleTranslationJob(Base):
    """字幕翻译任务。Phase B 新增——把 SubtitleJob 的英文输出 + bundle/CDN 上现有英文字幕
    都翻成 zh-CN（或其他目标语言）。gpt-4o 批量翻译，结果写回同 schema 的 JSON。

    source_subtitle_url：输入字幕 JSON 的 URL（必须是 Polly SubtitleDocument 格式）
    target_lang：默认 zh-CN
    result_subtitle_url：成功后的产物（带 translation 字段）
    """

    __tablename__ = "subtitle_translation_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # UUID4
    source_subtitle_url: Mapped[str] = mapped_column(String(2048))
    target_lang: Mapped[str] = mapped_column(String(16), default="zh-CN", index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    result_subtitle_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    segments_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


# ====== 用户 + 订阅 + 配额 ======

class User(Base):
    """用户。Phase B 初版：Apple ID（sub claim）作主键，邮箱可选。
    没真做 Apple Sign in 验签——服务端只接收 iOS 客户端透传的 sub。
    上架前换成 Apple JWKS 验签 + nonce。
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)  # Apple sub
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tier: Mapped[str] = mapped_column(String(16), default="free", index=True)
    # free / plus / pro
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Subscription(Base):
    """订阅记录。一个 user 同时只有一条 active 订阅。"""

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    tier: Mapped[str] = mapped_column(String(16))  # plus / pro
    apple_transaction_id: Mapped[str] = mapped_column(String(64), index=True)
    apple_original_id: Mapped[str] = mapped_column(String(64), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class QuotaUsage(Base):
    """每日配额计数。复合主键 (user_id, day, kind)，按日重置。
    匿名用户走 client_fp 当 user_id。
    """

    __tablename__ = "quota_usage"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYY-MM-DD UTC
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    # 取值：ai_explain / ai_word / ai_chat / pronunciation
    count: Mapped[int] = mapped_column(Integer, default=0)


# ====== 跟读评分 ======

class PronunciationScore(Base):
    """单次跟读评分记录。骨架：暂存 raw 转录 + naive 评分，未来引入 phoneme 级评分。"""

    __tablename__ = "pronunciation_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    video_id: Mapped[str] = mapped_column(String(64), index=True)
    segment_id: Mapped[int] = mapped_column(Integer)
    target_text: Mapped[str] = mapped_column(String(2048))
    spoken_text: Mapped[str] = mapped_column(String(2048))
    accuracy: Mapped[float] = mapped_column(Float)  # 0.0 - 1.0
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# ====== AI 对话练习 ======

class ChatSession(Base):
    """一次对话会话。messages 字段是 JSON 数组：[{"role": "user"|"assistant", "content": "..."}]"""

    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # uuid
    user_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    scenario: Mapped[str] = mapped_column(String(32), index=True)
    # airport / restaurant / interview / shopping / smalltalk
    messages: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


# ====== 阶段 3 数据复用 / 数据飞轮 ======


class Quiz(Base):
    """从已有内容派生的测验题。Q5 数据复用「① 派生新内容」。
    基于 `Explanation` 的 key_vocab / grammar_point 等结构化标注自动出题，
    一次性预生成 + 缓存——内容固定，题目永久复用。
    """

    __tablename__ = "quizzes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("contents.id", ondelete="CASCADE"), index=True
    )
    # 关联句 id（视频字幕句 / 图文段落句）；None = 内容级综合题
    segment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 题型：vocab_choice 词义选择 / grammar_choice 语法选择 / cloze 完形填空 / comprehension 理解题
    kind: Mapped[str] = mapped_column(String(32), index=True)
    question: Mapped[str] = mapped_column(String(2048))
    options: Mapped[list] = mapped_column(JSON, default=list)  # 选项文本列表
    answer_index: Mapped[int] = mapped_column(Integer)  # 正确选项在 options 中的下标
    rationale: Mapped[str] = mapped_column(String(2048))  # 答案解析
    source_model: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class WordOccurrence(Base):
    """词 → 内容反向索引。Q5 数据复用「③ 内容自我增强」。
    记录某个词（lemma）出现在哪条内容的哪一句，使查词卡能给出多个真实语境例句。
    派生/缓存表，可随时按全部内容重建。
    """

    __tablename__ = "word_occurrences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    word: Mapped[str] = mapped_column(String(64), index=True)  # 小写 lemma
    content_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("contents.id", ondelete="CASCADE"), index=True
    )
    segment_id: Mapped[int] = mapped_column(Integer)
    sentence: Mapped[str] = mapped_column(String(2048))  # 该词出现的真实句子
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
