"""账号与会话（V2）。

设计取舍：

- **口令哈希用标准库 `hashlib.scrypt`**，不引入 bcrypt/argon2。scrypt 是内存硬的，
  抗 GPU 爆破，参数按 RFC 7914 的常用取值（n=2^14, r=8, p=1）。存成
  `scrypt$n$r$p$salt_b64$hash_b64`，将来想换参数/算法能平滑迁移。
- **会话只用 httpOnly cookie**，token 存库。前端拿不到 token，XSS 也偷不走。
  每次请求校验过期时间，过期即删。
- **时序安全比较**用 `hmac.compare_digest`：口令比对、token 查找都不能用 `==`，
  否则能从响应时间上一点点猜出口令。这条很容易被忽略，但成本几乎为零。
- **开放模式**：库里一个用户都没有时不需要登录。这样刚部署完能直接用，
  V1 时代攒下的无主数据也不会变成谁也看不到的孤儿。第一个注册的用户认领它们。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, Request, Response

logger = logging.getLogger(__name__)

COOKIE_NAME = "pp_session"

# scrypt 参数：n=2^14 约 16MB 内存 / 次，本机实测单次 ~50ms，
# 对登录接口够用，对暴力破解足够贵。
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32

USERNAME_MIN = 3
USERNAME_MAX = 32
PASSWORD_MIN = 8
PASSWORD_MAX = 200
DISPLAY_NAME_MAX = 40


class AuthError(ValueError):
    """参数或凭据不合法。路由层映射成 400 / 401 / 403 / 409。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------
# 口令
# --------------------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))


def hash_password(password: str) -> str:
    """加盐哈希。每个用户独立 salt（由 os.urandom 提供）。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """校验口令。存储格式不认识时返回 False（不抛异常，避免登录接口 500）。"""
    try:
        algorithm, n_raw, r_raw, p_raw, salt_raw, digest_raw = stored.split("$")
        if algorithm != "scrypt":
            return False
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_unb64(salt_raw),
            n=int(n_raw),
            r=int(r_raw),
            p=int(p_raw),
            dklen=len(_unb64(digest_raw)),
        )
    except (ValueError, TypeError):
        return False
    # 必须用 compare_digest：`==` 会因为提前返回而泄漏前缀匹配长度
    return hmac.compare_digest(candidate, _unb64(digest_raw))


# --------------------------------------------------------------------------
# 参数校验
# --------------------------------------------------------------------------


def normalize_username(raw: str) -> str:
    username = (raw or "").strip().lower()
    if not (USERNAME_MIN <= len(username) <= USERNAME_MAX):
        raise AuthError(f"用户名长度需要 {USERNAME_MIN}-{USERNAME_MAX} 位")
    # 必须显式判 isascii()：str.isalnum() 对中日韩字符也返回 True，
    # 只写 isalnum() 的话「中文名」会被当成合法用户名放过去。
    if not all(ch.isascii() and (ch.isalnum() or ch in "_-") for ch in username):
        raise AuthError("用户名只能包含字母、数字、下划线和连字符")
    return username


def normalize_password(raw: str) -> str:
    password = raw or ""
    if len(password) < PASSWORD_MIN:
        raise AuthError(f"口令至少 {PASSWORD_MIN} 位")
    if len(password) > PASSWORD_MAX:
        raise AuthError(f"口令最长 {PASSWORD_MAX} 位")
    return password


def normalize_display_name(raw: str | None) -> str:
    return (raw or "").strip()[:DISPLAY_NAME_MAX]


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def new_share_token() -> str:
    return secrets.token_urlsafe(16)


def new_user_id() -> str:
    return "u_" + secrets.token_hex(3)


def new_album_id() -> str:
    return "al_" + secrets.token_hex(3)


# --------------------------------------------------------------------------
# cookie
# --------------------------------------------------------------------------


def set_session_cookie(response: Response, token: str, settings: Any) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(settings.session_ttl_days) * 86400,
        httponly=True,
        samesite="lax",
        secure=bool(settings.cookie_secure),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def session_cookie(request: Request) -> str | None:
    value = request.cookies.get(COOKIE_NAME)
    return value or None


def expiry_from_now(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")


def is_expired(value: str | None) -> bool:
    if not value:
        return True
    try:
        expires = datetime.fromisoformat(value)
    except ValueError:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# 依赖：当前用户
# --------------------------------------------------------------------------


def current_user(request: Request) -> dict[str, Any] | None:
    """取当前用户；未登录返回 None。**不抛异常**，调用方自己决定要不要 401。"""
    db = request.app.state.db
    token = session_cookie(request)
    if token:
        return db.user_for_session(token)
    return None


def user_or_401(request: Request) -> dict[str, Any] | None:
    """受保护接口统一用它。

    - 已登录 → 返回 user
    - 未登录且**库里一个用户都没有**（开放模式）→ 返回 None，放行。
      这是为了让「刚部署完还没建账号」能直接用，V1 时代的数据也不会变成孤儿。
    - 未登录但有用户存在 → 401

    注意不能写成 `require_user()` 那样只转发一下：开放模式这个分支必须在这里判，
    否则每个路由都得自己记一遍「什么时候可以没有 user」。
    """
    user = current_user(request)
    if user is None and not request.app.state.db.is_open_mode():
        raise HTTPException(status_code=401, detail="需要登录")
    return user
