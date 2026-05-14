"""每日 AI 端点配额。骨架，目前规则：

| tier  | ai_explain | ai_word | ai_chat | pronunciation |
|-------|------------|---------|---------|---------------|
| free  | 20         | 30      | 5       | 10            |
| plus  | 200        | 500     | 50      | 100           |
| pro   | unlimited  | unl.    | 200     | 500           |

匿名（没 user_id）按免费用户算，按 client_fp 计数。
真上线前换：
- 接 Apple sign in → users.id = sub claim
- Subscription Active 检查走 Apple ASN1 receipt verification
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import QuotaUsage, Subscription, User

TIER_LIMITS = {
    "free": {"ai_explain": 20, "ai_word": 30, "ai_chat": 5, "pronunciation": 10},
    "plus": {"ai_explain": 200, "ai_word": 500, "ai_chat": 50, "pronunciation": 100},
    "pro":  {"ai_explain": -1, "ai_word": -1, "ai_chat": 200, "pronunciation": 500},  # -1 = 不限
}


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _quota_key(request: Request, user_id: str | None) -> str:
    if user_id:
        return f"u:{user_id}"
    # 匿名：IP + UA hash 当 key（跟反馈端点的 client_fp 同款）
    xff = request.headers.get("x-forwarded-for")
    ip = xff.split(",")[0].strip() if xff else (request.client.host if request.client else "")
    ua = request.headers.get("user-agent", "")
    fp = hashlib.sha256(f"{ip}|{ua}".encode()).hexdigest()[:32]
    return f"a:{fp}"


def _user_tier(db: Session, user_id: str | None) -> str:
    if not user_id:
        return "free"
    user = db.get(User, user_id)
    if user is None:
        return "free"
    # 兜底校验：subscription 过期了 tier 自动降级到 free（避免漏更新）
    if user.tier != "free":
        active_sub = (
            db.query(Subscription)
            .filter(Subscription.user_id == user_id, Subscription.is_active.is_(True))
            .order_by(Subscription.expires_at.desc())
            .first()
        )
        if active_sub is None or active_sub.expires_at < datetime.now(timezone.utc):
            return "free"
    return user.tier


def consume(kind: str):
    """生成 FastAPI dependency：检查配额、+1、不够就 402（Payment Required）+ paywall hint。

    用法：
        @router.post(..., dependencies=[Depends(consume("ai_explain"))])
    """

    async def _dep(
        request: Request,
        x_polly_user: str | None = None,  # 客户端通过 X-Polly-User 头透传 Apple sub
        db: Session = Depends(get_db),
    ) -> None:
        # 这里 user_id 从 header 取——Phase B 暂时这样；上架前接 Apple JWKS 验签
        user_id = request.headers.get("x-polly-user") or None

        tier = _user_tier(db, user_id)
        limit = TIER_LIMITS.get(tier, TIER_LIMITS["free"]).get(kind, 0)
        if limit < 0:
            return  # 不限

        key = _quota_key(request, user_id)
        day = _utc_day()

        # upsert + 原子 +1。SQLite 没真 upsert，先 get 后 add，并发量大切 PG 用 INSERT ON CONFLICT
        row = db.get(QuotaUsage, (key, day, kind))
        if row is None:
            row = QuotaUsage(user_id=key, day=day, kind=kind, count=0)
            db.add(row)
            db.flush()

        if row.count >= limit:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "error": "quota_exceeded",
                    "kind": kind,
                    "tier": tier,
                    "limit": limit,
                    "upgrade_to": "plus" if tier == "free" else "pro",
                },
            )

        row.count += 1
        db.commit()

    return _dep
