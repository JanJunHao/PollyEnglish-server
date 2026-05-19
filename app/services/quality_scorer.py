"""Q1 视频质量打分器（阶段 1 视频线）。

对精读产品，"高质量"= 能不能当英语教材，不是画质。打分器分三级，越往后越贵，
便宜的先筛；本模块不做 L3（安全/合规交给采集连接器的版权过滤）。

  L0 元数据过滤（纯规则，0 成本）
      时长是否在合理区间、来源/频道是否可信。明显垃圾直接淘汰。

  L1 音频可教性（核心）
      聚合 Whisper 每段返回的 avg_logprob / no_speech_prob / compression_ratio，
      算出"音频可教性分"。转录置信度低 = 含糊/吵闹 = 坏教材。
      ASR 输出一份两用：subtitle_pipeline 的 tier3 转录时已把这些指标写进
      subtitle JSON 的 asr_meta，本模块直接复用，不重复转录。

  L2 教学价值（纯算法）
      复用 cc_pipeline/cefr_grader.py 评 CEFR 难度；算语速 WPM；难度是否连贯。
      LLM-as-judge 评信息密度为可选项，默认关闭以省 token。

综合分 → 质量档（publish / review / reject），接入 Content.status 流转。
L1 分写入 Content.audio_teachability，综合分写入 Content.quality_score。

所有子分均规整到 0.0-1.0（越高越好），便于加权与阈值判断。
"""

from __future__ import annotations

import json
import logging
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

# cefr_grader 在 scripts/cc_pipeline 下，确保可 import
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

log = logging.getLogger("polly.quality_scorer")


# =========================================================================
# 配置常量（集中放这里，便于调参 / 单测覆盖）
# =========================================================================

# ---- L0 元数据规则 ----
# 时长合理区间（秒）：太短没精读价值，太长不适合移动端单次学习
MIN_DURATION_SECONDS = 60
MAX_DURATION_SECONDS = 60 * 60  # 1 小时
# 长讲座放宽时长上限：MIT OCW / Open Yale 这类整节大课普遍 >60 分钟，
# 是高价值精读素材（自带人工校对逐字稿），不应被通用时长闸误拒。
# 对这类来源单独放宽到 3 小时——大学单节课极少超此值，仍能挡掉异常超长条目。
LONG_LECTURE_MAX_DURATION_SECONDS = 3 * 60 * 60  # 3 小时
# 可信来源白名单（命中 = L0 来源分满分；其余视频走规则判断）
TRUSTED_SOURCES = {
    "TED", "TED-Ed", "NASA", "VOA Learning English",
    "Internet Archive", "MIT OpenCourseWare", "Open Yale Courses",
}
# 长讲座来源白名单：命中这些来源时，L0 时长上限放宽到 LONG_LECTURE_MAX_DURATION_SECONDS。
# 仅放宽"上限"，下限与其他规则不变；这类来源整节大课长是常态而非质量问题。
LONG_LECTURE_SOURCES = {
    "MIT OpenCourseWare", "Open Yale Courses",
}

# ---- L1 音频可教性：Whisper 指标的"好/坏"边界 ----
# avg_logprob：转录平均对数概率，越接近 0 越好；< -1.0 基本是噪声/含糊
LOGPROB_GOOD = -0.30   # >= 此值算优秀
LOGPROB_BAD = -1.00    # <= 此值算很差
# no_speech_prob：该段被判为"非语音"的概率，越低越好
NO_SPEECH_GOOD = 0.10
NO_SPEECH_BAD = 0.60
# compression_ratio：gzip 压缩比，正常语音 ~1.5-2.4；过高=重复幻觉，过低=碎片
COMPRESSION_LOW = 1.2
COMPRESSION_HIGH_OK = 2.4
COMPRESSION_HIGH_BAD = 3.0

# ---- L2 教学价值 ----
# 语速 WPM 合理区间：太慢拖沓、太快听不清，教材最佳约 120-160
WPM_IDEAL_LOW = 110
WPM_IDEAL_HIGH = 170
WPM_HARD_LOW = 60
WPM_HARD_HIGH = 230

# ---- L2 LLM-as-judge 信息密度（可选项，默认关闭以省 token）----
# 打开后，L2 会额外调 LLM 评转录文本的"信息密度"（是否言之有物、含金量），
# 并把该子分并入 L2 总分。默认 False —— 规划文档把它标为可选项。
# 可在调用 score_content / score_l2 时传 use_llm_density=True 临时开启。
ENABLE_LLM_INFO_DENSITY = False
# 信息密度子分在 L2 加权中的占比（开启时其余子项等比缩放，保持总权重为 1）。
LLM_DENSITY_WEIGHT = 0.20
# LLM 信息密度评审用的模型，复用 classify 那档便宜模型。
LLM_DENSITY_MODEL = "gpt-4o-mini"

# ---- 综合分权重（L0 是淘汰闸门，不进加权；过闸后按 L1/L2 加权）----
# 音频可教性是精读的命门，权重最高
WEIGHT_L1 = 0.60
WEIGHT_L2 = 0.40

# ---- 综合分 → 质量档阈值 ----
SCORE_PUBLISH = 0.65   # >= 直接上首页
SCORE_REJECT = 0.35    # <  直接拒绝
# 介于两者之间 → review（人工复审）

# 质量档 → Content.status 映射（沿用现有 status 取值）
TIER_TO_STATUS = {
    "publish": "published",
    "review": "review_pending",
    "reject": "review_pending",  # 不删数据，降级到待审，人工可挽救/确认删除
}


# =========================================================================
# 结果数据结构
# =========================================================================

@dataclass
class QualityResult:
    """打分器完整产出。"""
    # L0
    l0_passed: bool
    l0_reasons: list[str] = field(default_factory=list)  # 未过闸的具体原因
    # L1
    audio_teachability: float | None = None   # 0-1，写入 Content.audio_teachability
    l1_detail: dict = field(default_factory=dict)
    # L2
    teaching_value: float | None = None       # 0-1
    l2_detail: dict = field(default_factory=dict)
    # 综合
    quality_score: float | None = None        # 0-1，写入 Content.quality_score
    tier: str = "reject"                       # publish / review / reject

    @property
    def status(self) -> str:
        """对应的 Content.status。"""
        return TIER_TO_STATUS.get(self.tier, "review_pending")

    def to_dict(self) -> dict:
        return {
            "l0_passed": self.l0_passed,
            "l0_reasons": self.l0_reasons,
            "audio_teachability": self.audio_teachability,
            "teaching_value": self.teaching_value,
            "quality_score": self.quality_score,
            "tier": self.tier,
            "status": self.status,
            "l1_detail": self.l1_detail,
            "l2_detail": self.l2_detail,
        }


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _lerp_score(value: float, bad: float, good: float) -> float:
    """把一个指标线性映射到 0-1（good 端=1，bad 端=0）。
    自动处理 good > bad（越大越好）和 good < bad（越小越好）两种方向。
    """
    if good == bad:
        return 0.5
    return _clamp01((value - bad) / (good - bad))


# =========================================================================
# L0 — 元数据过滤（纯规则，0 成本）
# =========================================================================

def score_l0(*, duration_seconds: int | None, source: str | None,
             kind: str | None = "video") -> tuple[bool, list[str]]:
    """L0 元数据闸门。返回 (是否通过, 未通过原因列表)。

    明显垃圾直接淘汰，省下后面更贵的 L1/L2。
    """
    reasons: list[str] = []

    if kind and kind != "video":
        # 本打分器只处理视频形态；非视频直接放行（不归本模块管）
        return True, []

    dur = duration_seconds or 0
    # 长讲座来源（MIT OCW / Open Yale）整节大课长是常态，放宽时长上限。
    is_long_lecture = bool(source) and source in LONG_LECTURE_SOURCES
    max_dur = LONG_LECTURE_MAX_DURATION_SECONDS if is_long_lecture else MAX_DURATION_SECONDS
    if dur < MIN_DURATION_SECONDS:
        reasons.append(f"时长过短 {dur}s < {MIN_DURATION_SECONDS}s")
    elif dur > max_dur:
        reasons.append(f"时长过长 {dur}s > {max_dur}s")

    # 来源信誉：白名单外的来源不直接淘汰（避免误杀新连接器），
    # 仅在完全没来源信息时记一笔——交给 L1/L2 实测兜底。
    if not source:
        reasons.append("来源缺失")

    return (len(reasons) == 0), reasons


# =========================================================================
# L1 — 音频可教性（核心，复用 ASR 输出）
# =========================================================================

def score_l1_from_asr_meta(asr_meta: dict | None) -> tuple[float | None, dict]:
    """从 Whisper asr_meta 聚合音频可教性分（0-1）。

    asr_meta 结构（见 subtitle_pipeline._tier3_whisper）：
        {"model", "language", "duration",
         "segments": [{"avg_logprob", "no_speech_prob", "compression_ratio"}, ...]}

    无 asr_meta（字幕来自 manual/auto vtt，没有 Whisper 置信度）返回 (None, {...})——
    上层据此走"无 L1 分"的降级综合逻辑。
    """
    if not asr_meta or not asr_meta.get("segments"):
        return None, {"reason": "无 ASR 置信度元数据（字幕非 Whisper 转录）"}

    segs = asr_meta["segments"]
    logprobs, no_speechs, comps = [], [], []
    for s in segs:
        if s.get("avg_logprob") is not None:
            logprobs.append(float(s["avg_logprob"]))
        if s.get("no_speech_prob") is not None:
            no_speechs.append(float(s["no_speech_prob"]))
        if s.get("compression_ratio") is not None:
            comps.append(float(s["compression_ratio"]))

    if not logprobs:
        return None, {"reason": "ASR 元数据存在但无可用指标"}

    # ---- 1) avg_logprob：用均值（整体清晰度）----
    mean_logprob = statistics.fmean(logprobs)
    logprob_score = _lerp_score(mean_logprob, LOGPROB_BAD, LOGPROB_GOOD)

    # ---- 2) no_speech_prob：用 75 分位（抓"有相当多段是静音/噪声"的坏情况）----
    no_speech_p75 = (
        sorted(no_speechs)[int(len(no_speechs) * 0.75)] if no_speechs else 0.0
    )
    no_speech_score = _lerp_score(no_speech_p75, NO_SPEECH_BAD, NO_SPEECH_GOOD)

    # ---- 3) compression_ratio：双侧惩罚（过高=重复幻觉，过低=碎片化）----
    mean_comp = statistics.fmean(comps) if comps else 1.8
    if mean_comp <= COMPRESSION_LOW:
        comp_score = _lerp_score(mean_comp, 0.6, COMPRESSION_LOW)
    elif mean_comp <= COMPRESSION_HIGH_OK:
        comp_score = 1.0
    else:
        comp_score = _lerp_score(mean_comp, COMPRESSION_HIGH_BAD, COMPRESSION_HIGH_OK)

    # ---- 4) 坏段占比：avg_logprob 极低的段落比例，再做一次惩罚 ----
    bad_segs = sum(1 for lp in logprobs if lp <= LOGPROB_BAD)
    bad_ratio = bad_segs / len(logprobs)

    # 聚合：logprob 是主信号，no_speech / compression 是辅助；坏段比例做乘性衰减
    base = logprob_score * 0.55 + no_speech_score * 0.25 + comp_score * 0.20
    audio_score = _clamp01(base * (1.0 - 0.5 * bad_ratio))

    detail = {
        "n_segments": len(segs),
        "mean_avg_logprob": round(mean_logprob, 3),
        "no_speech_p75": round(no_speech_p75, 3),
        "mean_compression_ratio": round(mean_comp, 3),
        "bad_segment_ratio": round(bad_ratio, 3),
        "sub_scores": {
            "logprob": round(logprob_score, 3),
            "no_speech": round(no_speech_score, 3),
            "compression": round(comp_score, 3),
        },
    }
    return round(audio_score, 4), detail


# =========================================================================
# L2 — 教学价值（纯算法：CEFR + WPM + 难度连贯）
# =========================================================================

def _wpm_score(wpm: float) -> float:
    """语速 WPM → 0-1 分。理想区间满分，向两端线性衰减。"""
    if WPM_IDEAL_LOW <= wpm <= WPM_IDEAL_HIGH:
        return 1.0
    if wpm < WPM_IDEAL_LOW:
        return _lerp_score(wpm, WPM_HARD_LOW, WPM_IDEAL_LOW)
    return _lerp_score(wpm, WPM_HARD_HIGH, WPM_IDEAL_HIGH)


# =========================================================================
# L2 可选子项：LLM-as-judge 信息密度
# =========================================================================

# 信息密度评审 system prompt（复用 app/api/ai.py 的 OpenAI 调用风格）。
_INFO_DENSITY_SYSTEM = """你是英语精读教材评审。给定一段视频转录文本，评估它作为精读素材的"信息密度"。

信息密度高 = 言之有物、观点/事实/叙事密集、值得逐句精读；
信息密度低 = 大量口水话、寒暄、重复、填充词、空洞客套，精读价值低。

只输出严格 JSON，不要 markdown 包裹：
{"density": 0.0-1.0, "reason": "一句话"}

density 越高表示信息密度越高、越适合做精读教材。"""


def score_llm_info_density(full_text: str, *, model: str = LLM_DENSITY_MODEL) -> tuple[float | None, dict]:
    """用 LLM 评转录文本的信息密度，返回 (density 0-1, detail)。

    可选项——仅在 ENABLE_LLM_INFO_DENSITY 或显式 use_llm_density=True 时被调用。
    复用 app/api/ai.py 的 OpenAI 调用风格（同步 client，JSON object 模式）。
    任何失败（无 key / API 报错 / 非 JSON）返回 (None, {...})，调用方据此跳过该子项。
    """
    try:
        from app.config import get_settings
        settings = get_settings()
        if not settings.openai_api_key:
            return None, {"reason": "未配置 OPENAI_API_KEY，跳过 LLM 信息密度"}

        from openai import OpenAI
        client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
        )
        # 转录文本可能很长，截断到 ~6000 字符控成本——足够代表整体信息密度
        excerpt = full_text[:6000]
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _INFO_DENSITY_SYSTEM},
                {"role": "user", "content": f"转录文本：\n{excerpt}"},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        density = _clamp01(float(data.get("density", 0.0)))
        return round(density, 4), {
            "density": round(density, 4),
            "reason": data.get("reason", ""),
            "model": resp.model,
        }
    except Exception as e:  # noqa: BLE001 — LLM 失败不应阻塞 L2 打分
        log.warning("LLM 信息密度评审失败，跳过该子项: %s", e)
        return None, {"reason": f"LLM 评审失败: {e}"}


def score_l2(
    *,
    segments: list[dict],
    duration_seconds: int | None,
    use_llm_density: bool | None = None,
) -> tuple[float | None, dict]:
    """L2 教学价值（纯算法 + 可选 LLM 信息密度）。

    segments：字幕 segments（[{"text", "start", "end", ...}]），用来取转录全文与算时长。
    复用 cefr_grader 评 CEFR；算语速 WPM；评估难度是否连贯（分段 CEFR 方差）。

    use_llm_density：是否启用 LLM-as-judge 信息密度子项。
        None（默认）→ 取模块常量 ENABLE_LLM_INFO_DENSITY（默认 False，即关闭）；
        True → 强制开启；False → 强制关闭。
        关闭时 L2 行为与改造前完全一致。
    """
    if not segments:
        return None, {"reason": "无字幕 segments，无法评教学价值"}

    full_text = " ".join((s.get("text") or "").strip() for s in segments).strip()
    word_count = len(full_text.split())
    if word_count < 30:
        return None, {"reason": f"转录文本过短（{word_count} 词），不评分"}

    # ---- 时长：优先用 segments 末尾时间，其次用入参 ----
    span = 0.0
    try:
        span = max(float(s.get("end") or 0.0) for s in segments)
    except ValueError:
        span = 0.0
    dur = span or float(duration_seconds or 0)

    # ---- 1) 语速 WPM ----
    wpm = (word_count / dur * 60.0) if dur > 0 else 0.0
    wpm_s = _wpm_score(wpm)

    # ---- 2) CEFR 难度（复用 cefr_grader）----
    cefr = "UNKNOWN"
    composite = 0.0
    try:
        from scripts.cc_pipeline.cefr_grader import CEFRGrader
        grader = CEFRGrader()
        graded = grader.grade(full_text)
        cefr = graded.get("cefr", "UNKNOWN")
        composite = float(graded.get("composite_score") or 0.0)
    except Exception as e:  # noqa: BLE001 — textstat 缺失等不应阻塞打分
        log.warning("cefr_grader 不可用，L2 跳过难度评估: %s", e)

    # CEFR 难度本身不是"质量好坏"，但难度落在学习者覆盖区间（A2-C1）更适合做教材；
    # 过易/过难（composite 接近 1 或 6）略微降分。
    if composite <= 0:
        cefr_fit = 0.6  # 评不出，给中性分
    else:
        # composite 3.5（B1/B2 之间）为最佳，向两端衰减
        cefr_fit = _clamp01(1.0 - abs(composite - 3.5) / 3.0)

    # ---- 3) 难度连贯性：分块评 CEFR composite，方差小=连贯 ----
    coherence = 1.0
    chunk_composites: list[float] = []
    if composite > 0 and word_count >= 240:
        try:
            from scripts.cc_pipeline.cefr_grader import CEFRGrader
            grader = CEFRGrader()
            words = full_text.split()
            n_chunks = min(5, word_count // 120)
            chunk_size = word_count // n_chunks
            for i in range(n_chunks):
                chunk = " ".join(words[i * chunk_size:(i + 1) * chunk_size])
                g = grader.grade(chunk)
                cs = float(g.get("composite_score") or 0.0)
                if cs > 0:
                    chunk_composites.append(cs)
            if len(chunk_composites) >= 2:
                std = statistics.pstdev(chunk_composites)
                # 标准差 0 → 1.0；>= 1.5 个 CEFR 档 → 0
                coherence = _clamp01(1.0 - std / 1.5)
        except Exception as e:  # noqa: BLE001
            log.debug("难度连贯性评估失败: %s", e)

    # ---- 4) 成句率：平均每段词数，过短=碎片化字幕，不利精读 ----
    avg_words_per_seg = word_count / len(segments)
    sentence_s = _clamp01(avg_words_per_seg / 8.0)  # ~8 词/段算成句

    # ---- 纯算法子项加权（与改造前一致）----
    base_value = (
        wpm_s * 0.35 + cefr_fit * 0.25 + coherence * 0.25 + sentence_s * 0.15
    )

    sub_scores = {
        "wpm": round(wpm_s, 3),
        "cefr_fit": round(cefr_fit, 3),
        "coherence": round(coherence, 3),
        "sentence": round(sentence_s, 3),
    }

    # ---- 5) LLM-as-judge 信息密度（可选项，默认关闭）----
    # 开启时：把信息密度按 LLM_DENSITY_WEIGHT 并入 L2，其余子项等比缩放，总权重仍为 1。
    # 关闭（默认）时：此分支完全不执行，L2 行为与改造前逐字节一致。
    enable_density = (
        ENABLE_LLM_INFO_DENSITY if use_llm_density is None else use_llm_density
    )
    density_detail: dict = {}
    if enable_density:
        density, density_detail = score_llm_info_density(full_text)
        if density is not None:
            # 算法子项整体缩放到 (1 - LLM_DENSITY_WEIGHT)，再叠加密度子项
            teaching_value = _clamp01(
                base_value * (1.0 - LLM_DENSITY_WEIGHT)
                + density * LLM_DENSITY_WEIGHT
            )
            sub_scores["llm_info_density"] = round(density, 3)
        else:
            # LLM 评审失败：退回纯算法 L2，不因可选子项失败而拖累打分
            teaching_value = _clamp01(base_value)
    else:
        teaching_value = _clamp01(base_value)

    detail = {
        "word_count": word_count,
        "duration_used": round(dur, 1),
        "wpm": round(wpm, 1),
        "cefr": cefr,
        "cefr_composite": round(composite, 2),
        "avg_words_per_segment": round(avg_words_per_seg, 1),
        "chunk_composites": [round(c, 2) for c in chunk_composites],
        "sub_scores": sub_scores,
    }
    if density_detail:
        detail["llm_info_density"] = density_detail
    return round(teaching_value, 4), detail


# =========================================================================
# 综合打分入口
# =========================================================================

def score_content(
    *,
    duration_seconds: int | None,
    source: str | None,
    segments: list[dict] | None,
    asr_meta: dict | None = None,
    kind: str | None = "video",
    use_llm_density: bool | None = None,
) -> QualityResult:
    """完整打分流程：L0 闸门 → L1 音频可教性 → L2 教学价值 → 综合分 + 质量档。

    Args:
        duration_seconds: 视频时长（秒）
        source: 来源/频道名
        segments: 字幕 segments（Polly SubtitleDocument 的 segments 字段）
        asr_meta: Whisper 转录的置信度元数据（subtitle JSON 的 asr_meta 字段）；
                  字幕来自 manual/auto vtt 时为 None，L1 走降级逻辑。
        kind: 内容形态，仅 'video' 走完整打分。
        use_llm_density: 是否在 L2 启用 LLM 信息密度子项；None=取模块默认（关闭）。

    Returns:
        QualityResult —— 含 audio_teachability / quality_score / tier / status。
    """
    result = QualityResult(l0_passed=False)

    # ---- L0 ----
    passed, reasons = score_l0(
        duration_seconds=duration_seconds, source=source, kind=kind
    )
    result.l0_passed = passed
    result.l0_reasons = reasons
    if not passed:
        # L0 未过闸：直接 reject，不再花 L1/L2 的钱
        result.tier = "reject"
        result.quality_score = 0.0
        return result

    # ---- L1 音频可教性 ----
    audio_score, l1_detail = score_l1_from_asr_meta(asr_meta)
    result.audio_teachability = audio_score
    result.l1_detail = l1_detail

    # ---- L2 教学价值 ----
    teaching_value, l2_detail = score_l2(
        segments=segments or [], duration_seconds=duration_seconds,
        use_llm_density=use_llm_density,
    )
    result.teaching_value = teaching_value
    result.l2_detail = l2_detail

    # ---- 综合分 ----
    # 三种情况：
    #   1) L1 + L2 都有 → 正常加权
    #   2) 只有 L2（manual/auto 字幕，无 Whisper 元数据）→ 用 L2 当综合分，
    #      但因缺少音频实测，封顶 publish 略保守（最高给 review 边界以上一点）
    #   3) 都没有 → 无法评分，落 review 让人工兜底
    if audio_score is not None and teaching_value is not None:
        composite = audio_score * WEIGHT_L1 + teaching_value * WEIGHT_L2
    elif teaching_value is not None:
        # 无音频实测：以 L2 为准，但不盲目相信，轻微下调
        composite = teaching_value * 0.85
        result.l1_detail["note"] = "无 Whisper 元数据，综合分仅基于 L2 并下调"
    elif audio_score is not None:
        composite = audio_score * 0.85
    else:
        # 什么都评不出 → 交人工
        result.quality_score = None
        result.tier = "review"
        return result

    composite = round(_clamp01(composite), 4)
    result.quality_score = composite

    # ---- 质量档 ----
    if composite >= SCORE_PUBLISH:
        result.tier = "publish"
    elif composite < SCORE_REJECT:
        result.tier = "reject"
    else:
        result.tier = "review"
    return result


# =========================================================================
# 便捷封装：从 subtitle JSON 直接打分
# =========================================================================

def score_from_subtitle_doc(
    subtitle_doc: dict, *, duration_seconds: int | None, source: str | None,
    kind: str | None = "video", use_llm_density: bool | None = None,
) -> QualityResult:
    """输入一份 Polly SubtitleDocument（含 segments，可选 asr_meta），直接打分。"""
    return score_content(
        duration_seconds=duration_seconds,
        source=source,
        segments=subtitle_doc.get("segments") or [],
        asr_meta=subtitle_doc.get("asr_meta"),
        kind=kind,
        use_llm_density=use_llm_density,
    )


def load_subtitle_doc(subtitle_url: str | None) -> dict | None:
    """按 subtitle_url 取回 SubtitleDocument JSON。

    支持三种形态：
      - 本地路径
      - http(s) URL（远程 storage / cloudflare tunnel）
      - 形如 .../static/subtitles/<id>.json 时优先尝试本地 cdn-staging/subtitles/<id>.json
    取不到返回 None（上层走降级）。
    """
    if not subtitle_url:
        return None

    # 1) 直接当本地路径
    p = Path(subtitle_url)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    # 2) 从 URL 末段文件名回查本地 cdn-staging（dev 环境字幕都落在这里）
    fname = subtitle_url.rstrip("/").split("/")[-1]
    if fname.endswith(".json"):
        local = _REPO_ROOT / "cdn-staging" / "subtitles" / fname
        if local.exists():
            try:
                return json.loads(local.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                return None

    # 3) 远程 URL
    if subtitle_url.startswith("http"):
        try:
            import httpx
            resp = httpx.get(subtitle_url, timeout=15, follow_redirects=True)
            if resp.status_code == 200:
                return resp.json()
        except Exception:  # noqa: BLE001
            return None

    return None
