"""系统与公开信息：/health、/options、/usage、/showcase。

这四条都**不用登录**（`/usage` 例外：它算的是某个账号的配额，只是开放模式下
返回一份「不设限」的空结果）。前端启动时靠 `/health` 的 `mode="open"` 判断
要不要显示登录入口，别改这个字段。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request

from ... import auth as auth_lib
from ... import voices as voice_catalog
from ...models import (
    HealthResponse,
    OptionItem,
    OptionsResponse,
    ShareView,
    UsageResponse,
)
from ...voices import DURATIONS, LEVELS, VOICES
from .shared import (
    router,
    _build_share_view,
    _db,
    _quota_window_start,
    _settings,
)


# --------------------------------------------------------------------------
# 基础信息
# --------------------------------------------------------------------------


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """**免登录**：前端要靠它判断该不该跳登录页。

    `mode="open"` 表示库里还没有任何用户（不需要登录），
    这时前端不显示登录入口 —— 否则会出现「要求登录但没有账号可登」的死循环。
    """
    settings = _settings(request)
    return HealthResponse(
        status="ok",
        version=settings.version,
        modes={"llm": settings.llm_mode, "tts": settings.tts_mode},
        mode="open" if _db(request).is_open_mode() else "auth",
    )


@router.get("/options", response_model=OptionsResponse)
async def options() -> OptionsResponse:
    """**免登录**：只是一份音色/时长目录，没有任何私人数据。

    首页（产品介绍页）在登录前也要能显示状态，401 会让它误报「后端未连接」。
    """
    return OptionsResponse(
        durations=DURATIONS,
        levels=LEVELS,
        voices=VOICES,
        languages=[
            OptionItem(value=code, label=label)
            for code, label in voice_catalog.LANGUAGE_LABELS.items()
        ],
    )


# --------------------------------------------------------------------------
# 配额用量
# --------------------------------------------------------------------------


@router.get("/usage", response_model=UsageResponse)
async def usage(request: Request):
    """今天还能生成几期。**需要登录**（没账号时没有配额概念）。

    有配额而不告诉用户还剩多少，等于让人撞 429 才知道 —— 那是很差的体验。
    """
    user = auth_lib.user_or_401(request)
    limit = int(_settings(request).daily_generation_limit or 0)
    if user is None:
        return UsageResponse(used=0, limit=0, remaining=None)

    since = _quota_window_start()
    used = _db(request).count_episodes_since(user["id"], since)
    resets_at = (datetime.fromisoformat(since) + timedelta(hours=24)).isoformat(
        timespec="seconds"
    )
    return UsageResponse(
        used=used,
        limit=limit,
        remaining=None if limit <= 0 else max(limit - used, 0),
        resets_at=None if limit <= 0 else resets_at,
    )


# --------------------------------------------------------------------------
# 首页的产品展示块（读真实产物，取不到就 404）
# --------------------------------------------------------------------------


@router.get("/showcase", response_model=ShareView)
async def get_showcase(request: Request):
    """首页展示用的一期。**免登录** —— 产品介绍页要在登录前就能看到真东西。

    - **登录了**：取自己最新一期已完成的（优先多语言），资源走受保护地址
    - **没登录**：取最新一期**公开分享**的（作者点过「分享」的那些），资源走 share 地址
    - 都没有 → `404`，前端把展示区整块隐藏

    为什么不做成「谁都能看所有别人的」：那是把私有数据默认公开了。
    想看真实产物就先点一下分享 —— 这正好也让分享功能有了用处。
    """
    db = _db(request)
    user = auth_lib.current_user(request)

    picked: dict[str, Any] | None = None
    if user:
        candidates, _ = db.list_episodes(
            limit=20, offset=0, status="completed", user_id=user["id"]
        )
        # 优先多语言：一屏就能把「视频 + 双语 + 切换器」三件事讲清楚
        def language_count(item: dict[str, Any]) -> int:
            return len((item.get("versions") or {}))

        picked = max(candidates, key=language_count, default=None)
    else:
        rows = db.query_public_episodes(limit=20)
        picked = rows[0] if rows else None

    if not picked:
        raise HTTPException(status_code=404, detail="还没有可以展示的成品")

    # 作者自己看：资源走 /api/episodes/{id}/…（有 cookie）
    # 别人看：走 /api/share/{token}/…（公开通道）
    is_owner = bool(user and picked.get("user_id") == user["id"])
    token = None
    if not is_owner:
        token = picked.get("share_token")
        if not token:
            raise HTTPException(status_code=404, detail="还没有可以展示的成品")

    return _build_share_view(request, picked, token, include_author=not is_owner)
