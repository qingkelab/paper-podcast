"""一键分享的开关 + 免登录的公开视图与公开资源。

- `/api/episodes/{id}/share*`：作者自己的开关（要登录）
- `/api/share/{token}*`：公开页读的东西，**全部免登录**

公开视图的响应体由 `shared.py::_build_share_view()` 统一构造（首页展示那块也用同一个
函数），这里只负责路由。公开页的资源必须另开一条路：原来的资源接口要登录，
公开页拿不到 cookie。
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Query, Request, Response
from fastapi.responses import FileResponse

from ... import auth as auth_lib
from ...models import Episode, ShareView
from ...services.pipeline import render_analysis_markdown, render_script_text
from .shared import (
    router,
    _IMAGE_CACHE,
    _build_share_view,
    _db,
    _download_filename,
    _landscape_path,
    _png_response,
    _quote,
    _ranged_response,
    _require_episode,
    _resolve_version,
    to_episode,
)


# --------------------------------------------------------------------------
# V2：一键分享
# --------------------------------------------------------------------------


@router.post("/episodes/{episode_id}/share", response_model=Episode)
async def enable_share(request: Request, episode_id: str):
    """设为公开并返回分享 token。

    **已有 token 时保持不变**：重复调用不能让已经发出去的链接失效 ——
    用户点两次「分享」，第二次就把刚才发给别人的链接弄失效，是最恼人的那种 bug。
    """
    record = _require_episode(request, episode_id)
    token = record.get("share_token") or auth_lib.new_share_token()
    _db(request).update_episode(episode_id, visibility="public", share_token=token)
    refreshed = _require_episode(request, episode_id)
    return to_episode(refreshed)


@router.delete("/episodes/{episode_id}/share", response_model=Episode)
async def disable_share(request: Request, episode_id: str):
    """取消公开：链接立即失效（token 一并清掉）。"""
    _require_episode(request, episode_id)
    _db(request).update_episode(episode_id, visibility="private", share_token=None)
    refreshed = _require_episode(request, episode_id)
    return to_episode(refreshed)


@router.post("/episodes/{episode_id}/share/reset", response_model=Episode)
async def reset_share(request: Request, episode_id: str):
    """换一个新 token（旧链接立即失效）。用于「链接被传出去了想收回」。"""
    _require_episode(request, episode_id)
    _db(request).update_episode(
        episode_id, visibility="public", share_token=auth_lib.new_share_token()
    )
    refreshed = _require_episode(request, episode_id)
    return to_episode(refreshed)


# --------------------------------------------------------------------------
# V2：公开分享（**全部免登录**）
# --------------------------------------------------------------------------


def _share_episode(request: Request, token: str) -> dict[str, Any]:
    record = _db(request).episode_by_share_token(token)
    if not record:
        raise HTTPException(status_code=404, detail="分享链接已失效")
    return record


@router.get("/share/{token}/video/poster")
async def share_video_poster(
    request: Request,
    token: str,
    lang: str | None = Query(None),
    orientation: str = Query("portrait"),
):
    """**公开通道的视频首帧**（免登录）。

    作者通道（`/api/episodes/{id}/video/poster`）一直有这条，公开通道漏了 ——
    后果不是「少一个字段」那么好听：展示页那一块是「首帧海报 + 播放按钮」，
    拿不到 poster 就只能退回**论文首页原图**（没有标题、不点播放永远是静止图），
    看起来就像「展示页没有视频」。两条通道必须给同一套字段。
    """
    record = _share_episode(request, token)
    landscape = (orientation or "portrait").lower() in ("landscape", "horizontal", "16:9")
    # 与其它公开资源路由同一套写法：`_resolve_version` 顺带校验语言是否存在
    _, version = _resolve_version(request, record, lang)
    stored = (version.get("video_landscape") or {}) if landscape else (version.get("video") or {})
    path = stored.get("poster")
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="这一版还没有封面静帧")
    return FileResponse(Path(path), media_type="image/png")


@router.get("/share/{token}", response_model=ShareView)
async def get_share(request: Request, token: str):
    """公开视图。**免登录**。

    刻意不是完整 Episode：不给 id / options / source_ref / raw_text / error，
    作者是谁也只给展示名。别人拿到链接能看能听，但看不到内部结构。
    """
    return _build_share_view(request, _share_episode(request, token), token)


@router.get("/share/{token}/video")
async def get_share_video(
    request: Request,
    token: str,
    lang: str | None = Query(None),
    orientation: str = Query("portrait"),
):
    record = _share_episode(request, token)
    _, version = _resolve_version(request, record, lang)
    landscape = (orientation or "portrait").lower() in ("landscape", "horizontal", "16:9")
    path = _landscape_path(version) if landscape else version.get("video_path")
    if not path or not Path(path).exists():
        raise HTTPException(
            status_code=404,
            detail="这一集还没有横版视频" if landscape else "这一集还没有视频",
        )
    return _ranged_response(request, Path(path), "video/mp4")


@router.get("/share/{token}/audio")
async def get_share_audio(request: Request, token: str, lang: str | None = Query(None)):
    record = _share_episode(request, token)
    _, version = _resolve_version(request, record, lang)
    path = version.get("audio_path")
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="这一集还没有音频")
    return _ranged_response(
        request, Path(path), mimetypes.guess_type(path)[0] or "audio/mpeg"
    )


@router.get("/share/{token}/cover")
async def get_share_cover(request: Request, token: str):
    record = _share_episode(request, token)
    cover_path = record.get("cover_path")
    if not cover_path:
        raise HTTPException(status_code=404, detail="这一集还没有封面")
    return _png_response(Path(cover_path))


@router.get("/share/{token}/figures/{figure_id}")
async def get_share_figure(request: Request, token: str, figure_id: str):
    record = _share_episode(request, token)
    for figure in record.get("figures") or []:
        if figure.get("id") == figure_id:
            return _png_response(Path(figure.get("path") or ""))
    raise HTTPException(status_code=404, detail="配图不存在")


@router.get("/share/{token}/illustration.png")
async def get_share_illustration_png(
    request: Request, token: str, lang: str | None = Query(None)
):
    record = _share_episode(request, token)
    _, version = _resolve_version(request, record, lang)
    stored = version.get("illustration") or {}
    if not stored.get("png_path"):
        raise HTTPException(status_code=404, detail="这一集还没有生成配图")
    return _png_response(Path(stored["png_path"]))


@router.get("/share/{token}/illustration.svg")
async def get_share_illustration_svg(
    request: Request, token: str, lang: str | None = Query(None)
):
    record = _share_episode(request, token)
    _, version = _resolve_version(request, record, lang)
    stored = version.get("illustration") or {}
    svg_path = stored.get("svg_path")
    if not svg_path or not Path(svg_path).exists():
        raise HTTPException(status_code=404, detail="这一集还没有生成配图")
    return FileResponse(
        Path(svg_path),
        media_type="image/svg+xml",
        headers={
            "Cache-Control": _IMAGE_CACHE,
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/share/{token}/script.txt")
async def get_share_script(request: Request, token: str, lang: str | None = Query(None)):
    record = _share_episode(request, token)
    language, version = _resolve_version(request, record, lang)
    content = render_script_text({**record, **version}, language)
    filename = _download_filename(record, language, "-脚本.txt")
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{_quote(filename)}"
        },
    )


@router.get("/share/{token}/analysis.md")
async def get_share_analysis(request: Request, token: str, lang: str | None = Query(None)):
    record = _share_episode(request, token)
    language, version = _resolve_version(request, record, lang)
    content = render_analysis_markdown({**record, **version}, language)
    filename = _download_filename(record, language, "-解读.md")
    return Response(
        content=content,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{_quote(filename)}"
        },
    )
