"""封面上那行大字标题（`PATCH /cover`）与重新合成视频。

**只写数据、不重新合成**：封面标题是烘焙进 MP4 的画面，改完要另行调
`POST /video/rebuild`（几十秒）—— 分两步是让用户先把标题改满意了再合成一次。
`headline` 逐语言一份，`paper_title` 整集共用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ...models import Episode
from ...services.pipeline import LanguageNotFound
from ...services.video import VideoError, normalize_hook
from .shared import (
    router,
    _db,
    _primary_language,
    _queue,
    _require_episode,
    _resolve_version,
    to_episode,
)


@router.post("/episodes/{episode_id}/video/rebuild", response_model=Episode)
async def rebuild_video(
    request: Request,
    episode_id: str,
    lang: str | None = Query(None),
    orientation: str = Query("portrait"),
):
    """用现有素材重新合成视频（配图人工校正后用）。

    音频、脚本、解读都不动，只重新渲染画面并编码。**复用上次的画面分配**，
    不再调用模型 —— 否则「我只转了一张图，怎么画面全变了」。

    双语集要用 `?lang=` 指定重合成哪一版；不带则重合成主语言那一版。
    `?orientation=landscape` 产出横版（1920×1080），竖版那一份原样保留 ——
    两种画幅可以同时存在，前端给一个切换。

    重新合成是重活（渲染 + 编码，约 20 秒），所以同步等待而不是丢进队列：
    调用方（前端）要明确知道什么时候能看到新视频。
    """
    record = _require_episode(request, episode_id)
    # 先校验语言：未知语言应当是 404，而不是走到「还没有视频」的 409
    _resolve_version(request, record, lang)
    if not record.get("video_path") and not (record.get("versions") or {}):
        raise HTTPException(status_code=409, detail="这一集还没有视频，无法重新合成")

    if orientation not in ("portrait", "landscape"):
        raise HTTPException(status_code=400, detail=f"未知画幅：{orientation}")

    try:
        await _queue(request).pipeline.rebuild_video(episode_id, lang, orientation)
    except LanguageNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VideoError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    refreshed = _db(request).get_episode(episode_id)
    assert refreshed is not None
    return to_episode(refreshed)


class CoverTitleRequest(BaseModel):
    """封面标题的自定义（两条都只影响**封面帧**）。

    - `headline`：封面上的大字（爆款标题）。**逐语言一份**，空串表示清掉自定义、
      退回显示论文原题；超长会被 `normalize_hook` 按分句收短（和模型写的那条同一套规则）。
    - `paper_title`：封面下方那行小字的论文原题，也就是这一集的名字（全站都用它）。
      不传 = 不动；传空串 = 不动（标题不能是空的）。
    """

    headline: str | None = None
    paper_title: str | None = None


@router.patch("/episodes/{episode_id}/cover", response_model=Episode)
async def update_cover_title(request: Request, episode_id: str, payload: CoverTitleRequest):
    """改封面上的标题（大字爆款标题 / 论文原题）。

    **只写数据、不重新合成**：视频是把画面烘焙进 MP4 的，改完标题要调
    `POST /video/rebuild` 才看得到新标题。分开两个接口是因为重新合成要几十秒，
    用户可能想先把标题改满意了再合成一次，而不是每敲一次字就重跑一遍。

    双语集的 `headline` 逐语言各一份，用 `?lang=` 指定改哪一版；
    `paper_title` 是这一集的名字（跨语言共用），改它对两种语言都生效。
    """
    record = _require_episode(request, episode_id)
    version = _resolve_version(request, record, lang=None)[1]
    # 语言取这一版自己的语言：中文标题按字数收、英文按词数收
    language = str(version.get("language") or record.get("language") or "zh")
    stored_video = dict(version.get("video") or {})

    fields: dict[str, Any] = {}
    # 改标题 = 视频里那行字**暂时对不上了**。打上这个标记，前端才会就地给出
    # 「重新合成视频」的按钮 —— 否则用户看到的是一句「点重新合成后生效」，
    # 而那个按钮平时根本不显示（它只挂在「配图过时」的提示条里）。
    # 重新合成时 `result.to_dict()` 会整份覆盖 video，标记自然被清掉。
    pending = False
    if payload.paper_title is not None and payload.paper_title.strip():
        # 论文原题也画在封面上（玻璃面板里那行小字），所以改它同样要重新合成
        new_title = payload.paper_title.strip()[:200]
        pending = pending or new_title != str(record.get("title") or "")
        fields["title"] = new_title

    if payload.headline is not None:
        headline = normalize_hook(payload.headline, language=language)
        pending = pending or headline != str(stored_video.get("hook") or "")
        stored_video["hook"] = headline

    # 只有当**真的改动了什么**（pending）或者改了标题字段时才写回 video：
    # 空 PATCH 不该在视频记录上留下痕迹
    if pending or payload.headline is not None:
        if pending:
            stored_video["title_pending"] = True
        versions = dict(record.get("versions") or {})
        if versions:
            primary = _primary_language(record)
            entry = dict(versions.get(language) or {})
            entry_video = dict(entry.get("video") or {})
            if payload.headline is not None:
                entry_video["hook"] = stored_video["hook"]
            if pending:
                entry_video["title_pending"] = True
            entry["video"] = entry_video
            versions[language] = entry
            fields["versions"] = versions
            # 顶层字段镜像主语言那一版（老前端和分享页读的是它）
            if language == primary:
                fields["video"] = stored_video
        else:
            fields["video"] = stored_video

    if fields:
        _db(request).update_episode(episode_id, **fields)
    refreshed = _db(request).get_episode(episode_id)
    assert refreshed is not None
    return to_episode(refreshed)


@router.get("/episodes/{episode_id}/video/poster")
async def get_video_poster(
    request: Request,
    episode_id: str,
    lang: str | None = Query(None),
    orientation: str = Query("portrait"),
):
    """视频第一帧的静帧（PNG）。前端拿它当 `<video poster>`。

    竖版和横版各有自己的一张（两个画面完全不同），所以也吃 `?orientation=`。
    老视频没有这张图 → 404，前端退回用论文首页当封面。
    """
    record = _require_episode(request, episode_id)
    _, version = _resolve_version(request, record, lang)
    landscape = (orientation or "portrait").lower() in ("landscape", "horizontal", "16:9")
    stored = version.get("video_landscape") or {} if landscape else version.get("video") or {}
    path = stored.get("poster")
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="这一版还没有封面静帧")
    return FileResponse(Path(path), media_type="image/png")
