"""AI 对话练习骨架。

固定场景列表 + multi-turn gpt-4o。会话状态存 chat_sessions.messages（JSON）。
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from openai import APIError, AsyncOpenAI
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import ChatSession
from app.quota import consume
from app.schemas import ChatMessage, ChatScenario, ChatTurnIn, ChatTurnOut
from app.security import rate_limit, require_api_key

router = APIRouter(
    prefix="/v1/chat",
    tags=["chat"],
    dependencies=[Depends(require_api_key), Depends(rate_limit("rate_limit_ai"))],
)

log = logging.getLogger("polly.chat")


SCENARIOS: dict[str, ChatScenario] = {
    "airport": ChatScenario(
        id="airport",
        label="机场值机",
        description="跟值机员办登机，托运行李、选座、晚点处理。",
        opener="Hi there! May I see your passport, please?",
    ),
    "restaurant": ChatScenario(
        id="restaurant",
        label="餐厅点餐",
        description="美式餐厅，菜单 / 推荐 / 过敏 / 买单。",
        opener="Welcome! How many in your party tonight?",
    ),
    "interview": ChatScenario(
        id="interview",
        label="英文面试",
        description="科技公司软件工程师常规问题。",
        opener="Thanks for coming in. Could you start by telling me about yourself?",
    ),
    "shopping": ChatScenario(
        id="shopping",
        label="商店购物",
        description="服装店 / 超市，问尺码、退换、找东西。",
        opener="Hi, can I help you find anything in particular today?",
    ),
    "smalltalk": ChatScenario(
        id="smalltalk",
        label="日常闲聊",
        description="天气、周末、爱好这些没压力的开场话题。",
        opener="Hey, beautiful weather we're having, isn't it?",
    ),
}


def _system_prompt(scenario: ChatScenario) -> str:
    return f"""你扮演一名英语母语者，正在跟一位英语学习者做 {scenario.label} 场景的口语练习。

要求：
- 全程用英语回应，自然、亲切
- 句子长度匹配学习者水平（B1-B2）；他用简单句你也用简单句
- 必要时温和纠正明显错误（错时态/词性），但每轮最多 1 个纠正点
- 每轮回应 1-3 句，引导对话继续
- 绝对不要：解释作为 AI 如何如何 / 切回中文 / 给免责声明

场景：{scenario.description}"""


@router.get("/scenarios", response_model=list[ChatScenario])
def list_scenarios() -> list[ChatScenario]:
    return list(SCENARIOS.values())


@router.post("/turn", response_model=ChatTurnOut, dependencies=[Depends(consume("ai_chat"))])
async def turn(body: ChatTurnIn, db: Session = Depends(get_db)) -> ChatTurnOut:
    settings = get_settings()
    if not settings.openai_api_key:
        raise HTTPException(status_code=503, detail="OPENAI_API_KEY not configured")
    scenario = SCENARIOS.get(body.scenario)
    if scenario is None:
        raise HTTPException(status_code=400, detail="unknown scenario")

    # 取 / 建会话
    if body.session_id:
        session = db.get(ChatSession, body.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
    else:
        session = ChatSession(
            id=str(uuid.uuid4()),
            user_id=body.user_id,
            scenario=body.scenario,
            messages=[{"role": "assistant", "content": scenario.opener}],
        )
        db.add(session)
        db.flush()

    # 把用户这轮加进 history
    history = list(session.messages or [])
    history.append({"role": "user", "content": body.message})

    # 调 gpt-4o
    client = AsyncOpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url or None,
    )
    try:
        resp = await client.chat.completions.create(
            model=settings.openai_explain_model,
            messages=[{"role": "system", "content": _system_prompt(scenario)}, *history],
            temperature=0.7,
            max_tokens=300,
        )
    except APIError as exc:
        log.exception("chat turn failed")
        raise HTTPException(status_code=502, detail=f"openai: {exc}") from exc

    reply = (resp.choices[0].message.content or "").strip()
    history.append({"role": "assistant", "content": reply})

    # 必须重新赋值整个 list，SQLAlchemy 才会标 dirty（JSON 列默认 in-place 改不会触发）
    session.messages = history
    db.commit()

    return ChatTurnOut(
        session_id=session.id,
        reply=reply,
        messages=[ChatMessage(**m) for m in history],
        model=resp.model,
    )
