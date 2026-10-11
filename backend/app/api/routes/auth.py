"""账号与会话：/auth/register、/login、/logout、/password、/me。

口令哈希、cookie 签发这些实现在 `app/auth.py`，这里只管 HTTP 层。
两条别动的东西：登录失败必须走限流（`LoginThrottle`），注册时第一个用户
要认领无主单集（`claim_orphan_episodes`）。
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import HTTPException, Request, Response

from ... import auth as auth_lib
from ...models import (
    LoginRequest,
    PasswordChangeRequest,
    RegisterRequest,
    User,
)
from .shared import router, _db, _settings

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# V2：账号与会话
# --------------------------------------------------------------------------


def _user_payload(user: dict[str, Any]) -> dict[str, Any]:
    """对外只给这几个字段。**password_hash 绝不出现** —— 这里白名单式构造，
    不是 `{**user}` 再删，免得以后加了字段（比如重置口令用的 token）被顺手带出去。"""
    return {
        "id": user["id"],
        "username": user["username"],
        "display_name": user.get("display_name") or "",
        "created_at": user["created_at"],
    }


@router.post("/auth/register", status_code=201, response_model=User)
async def register(request: Request, payload: RegisterRequest, response: Response):
    """注册即登录（直接下发会话 cookie）。

    **第一个注册成功的用户会认领所有无主单集** —— V1 时代的数据不会变成孤儿。
    """
    settings = _settings(request)
    db = _db(request)

    if settings.signup_code and not hmac.compare_digest(
        payload.signup_code or "", settings.signup_code
    ):
        raise HTTPException(status_code=403, detail="邀请码不正确")

    try:
        username = auth_lib.normalize_username(payload.username)
        password = auth_lib.normalize_password(payload.password)
    except auth_lib.AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    if db.get_user_by_username(username):
        raise HTTPException(status_code=409, detail="这个用户名已经被用了")

    first_user = db.is_open_mode()
    user = db.create_user(
        user_id=auth_lib.new_user_id(),
        username=username,
        password_hash=auth_lib.hash_password(password),
        display_name=auth_lib.normalize_display_name(payload.display_name),
    )

    if first_user:
        claimed = db.claim_orphan_episodes(user["id"])
        logger.info("首个账号 %s 认领了 %d 集无主数据", username, claimed)

    token = auth_lib.new_session_token()
    db.create_session(
        token=token,
        user_id=user["id"],
        expires_at=auth_lib.expiry_from_now(settings.session_ttl_days),
    )
    auth_lib.set_session_cookie(response, token, settings)
    return _user_payload(user)


@router.post("/auth/login", response_model=User)
async def login(request: Request, payload: LoginRequest, response: Response):
    """登录。用户名不存在与口令错误返回**同一个**提示，别告诉对方哪个错了。

    失败次数超限会先被限流挡住（429）—— 否则这个接口就是个可以无限尝试的爆破入口。
    """
    settings = _settings(request)
    db = _db(request)
    throttle: auth_lib.LoginThrottle = request.app.state.login_throttle
    attempt_key = throttle.key(
        (payload.username or "").strip().lower(), auth_lib.client_key(request)
    )

    wait = throttle.retry_after(attempt_key)
    if wait:
        raise HTTPException(
            status_code=429,
            detail=f"登录尝试过于频繁，请 {max(wait // 60, 1)} 分钟后再试",
            headers={"Retry-After": str(wait)},
        )

    try:
        username = auth_lib.normalize_username(payload.username)
    except auth_lib.AuthError as exc:
        raise HTTPException(status_code=401, detail="用户名或口令不正确") from exc

    user = db.get_user_by_username(username)
    # 用户不存在时也跑一次哈希校验：否则「不存在」比「口令错」快得多，
    # 从响应时间上就能枚举出哪些用户名是真实存在的。
    stored = user["password_hash"] if user else auth_lib.hash_password("dummy-password")
    ok = auth_lib.verify_password(payload.password or "", stored)
    if not user or not ok:
        throttle.record_failure(attempt_key)
        raise HTTPException(status_code=401, detail="用户名或口令不正确")

    # 登录成功清掉失败记录：否则之前攒的失败会把之后的正常登录也一起锁住
    throttle.reset(attempt_key)
    # 顺手清过期会话（省一个定时任务）
    db.purge_expired_sessions()

    token = auth_lib.new_session_token()
    db.create_session(
        token=token,
        user_id=user["id"],
        expires_at=auth_lib.expiry_from_now(settings.session_ttl_days),
    )
    auth_lib.set_session_cookie(response, token, settings)
    return _user_payload(user)


@router.post("/auth/logout", status_code=204)
async def logout(request: Request) -> Response:
    """幂等：没登录也返回 204，前端不必先判断状态。"""
    token = auth_lib.session_cookie(request)
    if token:
        _db(request).delete_session(token)
    response = Response(status_code=204)
    auth_lib.clear_session_cookie(response)
    return response


@router.post("/auth/password", status_code=204)
async def change_password(request: Request, payload: PasswordChangeRequest) -> Response:
    """改自己的口令。

    **必须验旧口令**：否则会话 cookie 被偷走就等于永久接管账号。
    改完把**其他**会话全部删掉（保留当前这一个）——
    口令换了之后，别处还挂着旧会话是最常见的「明明改了密码还是被盗」。
    """
    user = auth_lib.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="需要登录")

    if not auth_lib.verify_password(payload.current_password or "", user["password_hash"]):
        raise HTTPException(status_code=401, detail="当前口令不正确")

    try:
        new_password = auth_lib.normalize_password(payload.new_password)
    except auth_lib.AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    db = _db(request)
    db.update_user_password(user["id"], auth_lib.hash_password(new_password))

    # 只删「其他」会话，保留当前这条 —— 不能先全删再补回，那样一旦漏补
    # 用户改完口令自己就被踢出去了
    db.delete_other_sessions(user["id"], auth_lib.session_cookie(request))
    return Response(status_code=204)


@router.get("/auth/me", response_model=User)
async def me(request: Request):
    """未登录 401。前端启动时用它判断登录态。"""
    user = auth_lib.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="需要登录")
    return _user_payload(user)
