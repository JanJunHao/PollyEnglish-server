"""agent 的「动脑判断」环节 —— 全部由 LLM 驱动。

复用 app/config.py 的 OpenAI 配置与 app/api/ai.py 的调用风格
（chat.completions + response_format=json_object + 低 temperature）。

两类判断：
  judge_compliance(...)  模糊版权合规判定 → allow / deny / uncertain
  judge_quality(...)     候选质量评审（标题 + 逐字稿摘要）→ 是否值得入库

设计原则：判断不确定时一律倾向保守 —— 合规拿不准给 uncertain（转人工），
质量拿不准给 review（不自动入库）。绝不让 agent「乐观地」放行。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass

from openai import OpenAI

from app.config import get_settings

# ── 合规禁区（硬规则，先于 LLM 生效）────────────────────────────
# 任何来源 / 标题 / 描述命中这些关键词，直接 deny，不浪费 token 问 LLM。
FORBIDDEN_KEYWORDS = ["bbc", "ted talk", "ted-ed", "cnn", "netflix"]


def _client() -> OpenAI:
    """同步 OpenAI 客户端（脚本场景用同步版，不需要 async）。"""
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY 未配置，agent 的 LLM 判断环节无法运行")
    return OpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url or None,
    )


def _chat_json(system: str, user: str, temperature: float = 0.1) -> dict:
    """发一条 chat 请求并解析 JSON 返回；对齐 app/api/ai.py 的调用风格。"""
    settings = get_settings()
    client = _client()
    resp = client.chat.completions.create(
        model=settings.openai_classify_model,  # 判断类任务用便宜的 mini 即可
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=temperature,
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # LLM 偶尔返非 JSON —— 当作 uncertain 兜底，绝不静默放行
        return {}


# ── 1) 模糊版权合规判定 ────────────────────────────────────────
@dataclass
class ComplianceVerdict:
    decision: str   # allow | deny | uncertain
    reason: str
    by_rule: bool   # True = 硬规则裁定（未走 LLM）


_COMPLIANCE_SYSTEM = """你是 Polly 内容采集的版权合规审查员。Polly 是英语精读 App，
只能采集「公有领域」或「明确知识共享(CC)授权」的内容。

绝对禁区（命中即 deny）：BBC、TED 主频道演讲、CNN、Netflix，以及任何"保留所有权利"的商业内容。

判定规则：
- 来源是美国政府出版物（NASA / VOA / 国会图书馆等）→ 公有领域 → allow
- 来源标注明确 CC 授权（CC0 / CC BY / CC BY-SA / CC BY-NC-SA）→ allow
- 来源版权信息缺失、模糊、或像是受版权保护的商业内容 → uncertain
- 命中禁区 → deny

输出严格 JSON：
{"decision": "allow|deny|uncertain", "reason": "一句话中文说明"}

拿不准时一律给 uncertain，绝不乐观放行。"""


def judge_compliance(
    *,
    connector_key: str,
    license_note: str,
    title: str,
    source: str,
    description: str = "",
    attribution: str = "",
) -> ComplianceVerdict:
    """对一个候选条目做合规判定。

    先跑硬规则（禁区关键词），命中即 deny；否则交 LLM 评 allow/deny/uncertain。
    """
    # 硬规则：禁区关键词扫描（source / title / description / attribution）
    haystack = " ".join([source, title, description, attribution]).lower()
    for kw in FORBIDDEN_KEYWORDS:
        if kw in haystack:
            return ComplianceVerdict(
                decision="deny",
                reason=f"命中合规禁区关键词「{kw}」",
                by_rule=True,
            )

    user = (
        f"连接器：{connector_key}\n"
        f"连接器版权说明：{license_note}\n"
        f"候选标题：{title}\n"
        f"候选来源：{source}\n"
        f"署名信息：{attribution or '（无）'}\n"
        f"描述：{description[:600] or '（无）'}"
    )
    data = _chat_json(_COMPLIANCE_SYSTEM, user)
    decision = str(data.get("decision", "")).lower()
    if decision not in ("allow", "deny", "uncertain"):
        # LLM 返回异常 → 保守当 uncertain
        decision = "uncertain"
    return ComplianceVerdict(
        decision=decision,
        reason=data.get("reason", "（LLM 未给出理由，按 uncertain 处理）"),
        by_rule=False,
    )


# ── 2) 候选质量 LLM 评审 ───────────────────────────────────────
@dataclass
class QualityVerdict:
    verdict: str    # pass | review | reject
    score: float    # 0.0 - 1.0
    reason: str


_QUALITY_SYSTEM = """你是 Polly 内容质量评审。Polly 是面向中文母语者的英语精读 App，
"高质量"= 能不能当英语教材：语言清晰、信息成句、题材正向、适合学习者跟读精读。

给候选打 verdict：
- pass：明显适合精读，标题清晰、内容像有实质解说/叙述的英语材料
- review：模糊地带 —— 题材不确定、逐字稿摘要太短或像噪声、难度可能过高
- reject：明显不适合 —— 纯音乐/无解说、标题空洞、内容残缺、含不良题材

输出严格 JSON：
{"verdict": "pass|review|reject", "score": 0.0-1.0, "reason": "一句话中文说明"}

注意：你只做粗筛。入库后还有确定性的 Q1 质量打分器复核，所以拿不准就给 review。"""


def _transcript_excerpt(candidate: dict) -> str:
    """从候选里抽一段逐字稿摘要喂给 LLM。

    不同连接器字段不同：VOA 有 transcript（段落数组）；NASA 等只有 description。
    """
    transcript = candidate.get("transcript")
    if isinstance(transcript, list) and transcript:
        return " ".join(str(p) for p in transcript)[:1200]
    return str(candidate.get("description") or "")[:1200]


def judge_quality(candidate: dict) -> QualityVerdict:
    """对一个候选做质量 LLM 评审。"""
    title = str(candidate.get("title") or "(untitled)")
    excerpt = _transcript_excerpt(candidate)
    user = (
        f"标题：{title}\n"
        f"来源：{candidate.get('source') or '未知'}\n"
        f"时长(秒)：{candidate.get('duration_seconds') or '未知'}\n"
        f"逐字稿/描述摘要：{excerpt or '（无）'}"
    )
    data = _chat_json(_QUALITY_SYSTEM, user)
    verdict = str(data.get("verdict", "")).lower()
    if verdict not in ("pass", "review", "reject"):
        verdict = "review"  # 异常返回 → 保守进复核
    try:
        score = float(data.get("score", 0.0))
    except (TypeError, ValueError):
        score = 0.0
    return QualityVerdict(
        verdict=verdict,
        score=score,
        reason=data.get("reason", "（LLM 未给出理由）"),
    )
