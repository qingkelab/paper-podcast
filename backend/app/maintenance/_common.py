"""维护脚本的公共件：连库、列表、打印「要动什么」。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

BACKEND = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class Episode:
    id: str
    title: str
    status: str
    updated_at: str
    word_count: int
    duration_sec: float
    duration_min: int | None

    @property
    def drift(self) -> float | None:
        """实际时长相对「设定时长」的偏差（+0.3 = 超 30%）。"""
        if not self.duration_min:
            return None
        return self.duration_sec / (self.duration_min * 60) - 1


def connect() -> sqlite3.Connection:
    from app.config import Settings

    return sqlite3.connect(Settings().db_path)


def list_episodes(*, status: str | None = "completed", ids: Iterable[str] | None = None) -> list[Episode]:
    con = connect()
    con.row_factory = sqlite3.Row
    wanted = list(ids) if ids else None
    query = "select id, title, status, updated_at, script_json, options_json, audio_duration_sec from episodes"
    if status:
        query += f" where status='{status}'"
    query += " order by updated_at desc"
    out: list[Episode] = []
    for row in con.execute(query):
        if wanted and row["id"] not in wanted:
            continue
        script = json.loads(row["script_json"] or "{}")
        options = json.loads(row["options_json"] or "{}")
        out.append(
            Episode(
                id=row["id"],
                title=(row["title"] or "")[:44],
                status=row["status"],
                updated_at=(row["updated_at"] or "")[:16],
                word_count=int(script.get("word_count") or 0),
                duration_sec=float(row["audio_duration_sec"] or 0.0),
                duration_min=options.get("duration_min"),
            )
        )
    return out


def print_table(rows: list[Episode]) -> None:
    print(f"{'集':<12}{'段落/字数':<12}{'实际':<8}{'设定':<6}{'偏差':<8}{'标题'}")
    for row in rows:
        words = f"{row.word_count} 字"
        drift = row.drift
        print(
            f"{row.id:<12}{words:<12}{row.duration_sec/60:>5.2f}分 "
            f"{str(row.duration_min or '?'):>4}  "
            f"{(f'{drift*100:+.0f}%' if drift is not None else '-'):<8}{row.title}"
        )


def find(episode_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """取 (顶层 video, versions) —— **两份都要改**，只改顶层会被下次重合成冲掉。"""
    con = connect()
    con.row_factory = sqlite3.Row
    row = con.execute("select video_json, versions_json from episodes where id=?", (episode_id,)).fetchone()
    if row is None:
        raise SystemExit(f"没有这一集：{episode_id}")
    return json.loads(row["video_json"] or "{}"), json.loads(row["versions_json"] or "{}")
