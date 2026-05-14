from datetime import datetime

import warnings

from pydantic import BaseModel, ConfigDict, Field

# pydantic 2 在 BaseModel 上没有 `register` 属性，但仍会 UserWarning。
# 字段名要与 iOS Codable 一致（register），所以静音掉这条噪声。
warnings.filterwarnings("ignore", message='Field name "register"')


# ---------- contents ----------


class ContentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    author: str
    source: str
    duration_seconds: int
    cefr_level: str
    play_mode: str
    video_url: str | None
    youtube_id: str | None
    thumbnail_url: str
    subtitle_url: str | None
    vocabulary_url: str | None
    explanation_url: str | None
    categories: list[str]
    category_color_hex: int
    is_recommended: bool
    updated_at: datetime


class ContentsLatestOut(BaseModel):
    server_time: datetime
    version: int
    contents: list[ContentOut]


# ---------- ai/explain ----------
# schema 与 iOS ExplanationResult ([Polly/Polly/Models/Explanation.swift]) 严格对齐


class ExplainIn(BaseModel):
    sentence: str = Field(min_length=1, max_length=2000, description="目标句")
    video_id: str | None = None
    segment_id: int | None = Field(default=None, description="句 id；与 video_id 一起作为预生成缓存 key")
    video_title: str | None = Field(default=None, max_length=512)
    video_author: str | None = Field(default=None, max_length=256)
    video_source: str | None = Field(default=None, max_length=64)
    cefr_level: str | None = Field(default=None, max_length=8)
    context_before: str | None = Field(default=None, max_length=2000)
    context_after: str | None = Field(default=None, max_length=2000)


class VocabItem(BaseModel):
    word: str
    meaning: str
    register: str | None = None  # 正式 / 口语 / 俚语 / null
    examples: list[str] | None = None


class ExplainOut(BaseModel):
    sentence: str
    natural_translation: str
    core_explanation: str
    key_vocab: list[VocabItem]
    grammar_point: str | None = None
    cultural_note: str | None = None
    pronunciation_tip: str | None = None
    similar_expressions: list[str] | None = None
    model: str
    cached: bool = False


# ---------- ai/word ----------
# 与 iOS DictionaryService 的 WordEntry 对齐


class WordIn(BaseModel):
    word: str = Field(min_length=1, max_length=64)
    context: str | None = Field(default=None, max_length=500)


class WordDefinition(BaseModel):
    pos: str
    meaning: str


class WordOut(BaseModel):
    word: str
    phonetic: str
    level: str
    definitions: list[WordDefinition]
    model: str
    cached: bool = False


# ---------- ai/classify ----------


class ClassifyIn(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    author: str | None = None
    source: str | None = None
    subtitle_excerpt: str = Field(default="", max_length=2000)


class ClassifyOut(BaseModel):
    categories: list[str]
    confidence: float
    reason: str
    model: str


# ---------- pronunciation/score ----------


class PronunciationScoreIn(BaseModel):
    video_id: str = Field(min_length=1, max_length=64)
    segment_id: int
    target_text: str = Field(min_length=1, max_length=2000)
    spoken_text: str = Field(min_length=1, max_length=2000)
    user_id: str | None = None


class WordDiffItem(BaseModel):
    target: str
    spoken: str | None  # None = 漏读
    ok: bool


class PronunciationScoreOut(BaseModel):
    accuracy: float  # 0.0 - 1.0
    word_diff: list[WordDiffItem]
    feedback: str  # 自然语言点评


# ---------- chat/scenarios ----------


class ChatScenario(BaseModel):
    id: str
    label: str
    description: str
    opener: str  # AI 主动说的第一句


class ChatTurnIn(BaseModel):
    session_id: str | None = None  # None = 开新会话
    scenario: str = Field(pattern="^(airport|restaurant|interview|shopping|smalltalk)$")
    message: str = Field(min_length=1, max_length=2000)
    user_id: str | None = None


class ChatMessage(BaseModel):
    role: str  # user / assistant
    content: str


class ChatTurnOut(BaseModel):
    session_id: str
    reply: str
    messages: list[ChatMessage]
    model: str


# ---------- subtitles/jobs ----------


class SubtitleJobIn(BaseModel):
    youtube_id: str = Field(min_length=5, max_length=32, description="YouTube videoId")
    target_video_id: str | None = Field(
        default=None, max_length=64,
        description="可选：成功后把 result_url 写到 contents.subtitle_url 这个视频上",
    )


class SubtitleJobOut(BaseModel):
    id: str
    youtube_id: str | None
    status: str  # pending / running / done / failed
    result_subtitle_url: str | None = None
    segments_count: int | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


# ---------- translations/jobs ----------


class TranslationJobIn(BaseModel):
    source_subtitle_url: str = Field(
        min_length=8, max_length=2048,
        description="待翻译的 Polly SubtitleDocument JSON 的 URL（要 server 可访问）",
    )
    target_lang: str = Field(default="zh-CN", min_length=2, max_length=16)


class TranslationJobOut(BaseModel):
    id: str
    source_subtitle_url: str
    target_lang: str
    status: str  # pending / running / done / failed
    result_subtitle_url: str | None = None
    segments_count: int | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


# ---------- contents/feedback ----------


class FeedbackIn(BaseModel):
    kind: str = Field(pattern="^(wrong_category|poor_audio|poor_video|wrong_subtitle|other)$")
    note: str | None = Field(default=None, max_length=1024)


class FeedbackOut(BaseModel):
    accepted: bool
    feedback_count: int
    status: str  # 当前 content.status：published / review_pending / draft


# ---------- health ----------


class HealthOut(BaseModel):
    status: str
    db: str
