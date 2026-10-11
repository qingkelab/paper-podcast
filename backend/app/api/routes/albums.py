"""个人专辑：增删改查 + 单集归属（/albums/*）。

专辑是**分组不是容器**：删专辑只把单集的 `album_id` 置空，绝不删单集。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, Response

from ... import auth as auth_lib
from ...models import (
    Album,
    AlbumAssignRequest,
    AlbumDetail,
    AlbumWriteRequest,
)
from .shared import router, _asset_version, _db, _require_album, to_episode


# --------------------------------------------------------------------------
# V2：个人专辑
# --------------------------------------------------------------------------


def _cover_url_for_episode(record: dict[str, Any]) -> str | None:
    path = record.get("cover_path")
    if path and Path(path).exists():
        return f"/api/episodes/{record['id']}/cover?v={_asset_version(path)}"
    return None


def _album_payload(request: Request, album: dict[str, Any]) -> dict[str, Any]:
    """专辑摘要。封面取专辑里最新一集的封面。"""
    db = _db(request)
    episodes, _ = db.list_episodes(album_id=album["id"], limit=1, offset=0)
    cover_url = _cover_url_for_episode(episodes[0]) if episodes else None
    return {
        "id": album["id"],
        "title": album["title"],
        "description": album.get("description"),
        "episode_count": db.count_episodes_in_album(album["id"]),
        "cover_url": cover_url,
        "created_at": album["created_at"],
        "updated_at": album["updated_at"],
    }


@router.get("/albums", response_model=list[Album])
async def list_albums(request: Request):
    user = auth_lib.user_or_401(request)
    if user is None:
        return []
    return [_album_payload(request, album) for album in _db(request).list_albums(user["id"])]


@router.post("/albums", status_code=201, response_model=Album)
async def create_album(request: Request, payload: AlbumWriteRequest):
    user = auth_lib.user_or_401(request)
    if user is None:
        raise HTTPException(status_code=401, detail="需要登录")

    title = (payload.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="专辑名称不能为空")
    if len(title) > 60:
        raise HTTPException(status_code=400, detail="专辑名称最长 60 字")

    description = (payload.description or "").strip() or None
    album = _db(request).create_album(
        album_id=auth_lib.new_album_id(),
        user_id=user["id"],
        title=title,
        description=description[:200] if description else None,
    )
    return _album_payload(request, album)


@router.get("/albums/{album_id}", response_model=AlbumDetail)
async def get_album(request: Request, album_id: str):
    album = _require_album(request, album_id)
    payload = _album_payload(request, album)
    records, _ = _db(request).list_episodes(album_id=album_id, limit=100, offset=0)
    payload["episodes"] = [to_episode(r, include_large=False) for r in records]
    return payload


@router.patch("/albums/{album_id}", response_model=Album)
async def update_album(request: Request, album_id: str, payload: AlbumWriteRequest):
    album = _require_album(request, album_id)
    user = auth_lib.current_user(request)
    assert user is not None  # _require_album 已经保证了这一点

    fields: dict[str, Any] = {}
    if payload.title is not None:
        title = payload.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="专辑名称不能为空")
        fields["title"] = title[:60]
    if payload.description is not None:
        fields["description"] = payload.description.strip()[:200] or None

    _db(request).update_album(album_id, user_id=user["id"], **fields)
    refreshed = _db(request).get_album(album_id, user_id=user["id"])
    assert refreshed is not None
    return _album_payload(request, refreshed)


@router.delete("/albums/{album_id}", status_code=204)
async def delete_album(request: Request, album_id: str) -> Response:
    """只删专辑，**不删里面的单集**（单集的 album_id 置空）。"""
    album = _require_album(request, album_id)
    user = auth_lib.current_user(request)
    assert user is not None
    _db(request).delete_album(album["id"], user_id=user["id"])
    return Response(status_code=204)


@router.post("/albums/{album_id}/episodes", response_model=AlbumDetail)
async def assign_album_episodes(
    request: Request, album_id: str, payload: AlbumAssignRequest
):
    """把单集加入专辑。幂等：已经在里面的再传一次没有副作用。"""
    album = _require_album(request, album_id)
    user = auth_lib.current_user(request)
    assert user is not None

    ids = [item for item in (payload.episode_ids or []) if item]
    if not ids:
        raise HTTPException(status_code=400, detail="episode_ids 不能为空")
    if len(ids) > 100:
        raise HTTPException(status_code=400, detail="一次最多加 100 集")

    db = _db(request)
    # 只加自己的单集：别人的 id 直接忽略（不报错，也不泄漏它们存在）
    db.assign_episodes_to_album(album["id"], ids, user_id=user["id"])
    db.touch_album(album["id"])
    return await get_album(request, album_id)


@router.delete("/albums/{album_id}/episodes/{episode_id}", response_model=AlbumDetail)
async def remove_album_episode(request: Request, album_id: str, episode_id: str):
    album = _require_album(request, album_id)
    user = auth_lib.current_user(request)
    assert user is not None
    _db(request).remove_episode_from_album(album["id"], episode_id, user_id=user["id"])
    _db(request).touch_album(album["id"])
    return await get_album(request, album_id)
