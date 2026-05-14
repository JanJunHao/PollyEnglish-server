"""跟读评分骨架。

当前实现：naive word-level diff 算 accuracy。
未来升级（plan「跟读评分」）：
- 接 phoneme 级评分（espeak-ng / Wav2Vec2 alignment）
- 客户端直传音频，服务端跑 Whisper 转 spoken_text（现在让 iOS 自己转，省 quota）
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import PronunciationScore
from app.quota import consume
from app.schemas import PronunciationScoreIn, PronunciationScoreOut, WordDiffItem
from app.security import rate_limit, require_api_key

router = APIRouter(
    prefix="/v1/pronunciation",
    tags=["pronunciation"],
    dependencies=[Depends(require_api_key), Depends(rate_limit("rate_limit_ai"))],
)


_TOKEN = re.compile(r"[A-Za-z']+")


def _tokens(s: str) -> list[str]:
    return [w.lower() for w in _TOKEN.findall(s)]


def _word_diff(target: list[str], spoken: list[str]) -> list[WordDiffItem]:
    """简单 longest-common-subsequence 风格的对齐。
    朴素实现：双指针，target 词在 spoken 里出现就标 ok，否则标漏读。
    误读（spoken 有但 target 没有的词）这里不返回，留给前端展示。
    """
    diff: list[WordDiffItem] = []
    j = 0
    for w in target:
        # 在 spoken[j:] 里找 w，找到就消耗对应索引
        if j < len(spoken) and spoken[j] == w:
            diff.append(WordDiffItem(target=w, spoken=w, ok=True))
            j += 1
            continue
        # 往前看 2 个，容忍少量乱序
        peek = next((k for k in range(j, min(j + 3, len(spoken))) if spoken[k] == w), None)
        if peek is not None:
            diff.append(WordDiffItem(target=w, spoken=spoken[peek], ok=True))
            j = peek + 1
        else:
            diff.append(WordDiffItem(target=w, spoken=None, ok=False))
    return diff


def _feedback(acc: float, diff: list[WordDiffItem]) -> str:
    missed = [d.target for d in diff if not d.ok]
    if acc >= 0.95:
        return "完美！发音清晰，节奏自然。"
    if acc >= 0.8:
        msg = "很好，整体清晰。"
        if missed:
            msg += f" 注意这几个词：{', '.join(missed[:3])}。"
        return msg
    if acc >= 0.5:
        msg = "有进步空间。漏读或不清楚的词："
        msg += ", ".join(missed[:5]) if missed else "整体连贯但停顿多。"
        return msg
    return "差距较大，建议先慢速跟读几遍。"


@router.post("/score", response_model=PronunciationScoreOut, dependencies=[Depends(consume("pronunciation"))])
def score(body: PronunciationScoreIn, db: Session = Depends(get_db)) -> PronunciationScoreOut:
    target = _tokens(body.target_text)
    spoken = _tokens(body.spoken_text)
    if not target:
        return PronunciationScoreOut(accuracy=0.0, word_diff=[], feedback="目标文本为空。")

    diff = _word_diff(target, spoken)
    accuracy = sum(1 for d in diff if d.ok) / len(diff)
    feedback = _feedback(accuracy, diff)

    db.add(PronunciationScore(
        user_id=body.user_id,
        video_id=body.video_id,
        segment_id=body.segment_id,
        target_text=body.target_text,
        spoken_text=body.spoken_text,
        accuracy=accuracy,
    ))
    db.commit()

    return PronunciationScoreOut(accuracy=accuracy, word_diff=diff, feedback=feedback)
