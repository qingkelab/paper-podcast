"""SQLite 存储层。

刻意用标准库 sqlite3 而不是 ORM：本项目只有一张表，ORM 带来的迁移/版本负担
大于收益。WAL 模式 + 单连接加锁，对 MVP 的并发量（单机、串行 worker）足够。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id                 TEXT PRIMARY KEY,
    title              TEXT NOT NULL,
    source_type        TEXT NOT NULL,
    source_ref         TEXT,
    status             TEXT NOT NULL,
    stage_label        TEXT NOT NULL DEFAULT '',
    progress           INTEGER NOT NULL DEFAULT 0,
    error              TEXT,
    options_json       TEXT NOT NULL,
    paper_meta_json    TEXT,
    analysis_json      TEXT,
    script_json        TEXT,
    raw_text           TEXT,
    audio_path         TEXT,
    audio_duration_sec REAL,
    audio_bytes        INTEGER,
    podcast_task_id    TEXT,
    finished_round     INTEGER NOT NULL DEFAULT -1,
    retry_count        INTEGER NOT NULL DEFAULT 0,
    cover_path         TEXT,
    cover_width        INTEGER,
    cover_height       INTEGER,
    figures_json       TEXT,
    illustration_json  TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episodes_created ON episodes(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_status  ON episodes(status);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:8]


class Database:
    """极简存储层。所有方法都是线程安全的。"""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """给已存在的旧库补上后来新增的列。

        用 PRAGMA table_info 对比而不是直接 ALTER，否则重复执行会报错。
        本项目刻意不做完整迁移框架：加列是唯一需要的演进方式。
        """
        existing = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(episodes)").fetchall()
        }
        additions = {
            "cover_path": "TEXT",
            "cover_width": "INTEGER",
            "cover_height": "INTEGER",
            "figures_json": "TEXT",
            "illustration_json": "TEXT",
        }
        for column, sql_type in additions.items():
            if column not in existing:
                self._conn.execute(f"ALTER TABLE episodes ADD COLUMN {column} {sql_type}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- 内部 ----------

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    # ---------- 行 <-> dict ----------

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> dict[str, Any]:
        def load(value: Any) -> Any:
            if value in (None, ""):
                return None
            try:
                return json.loads(value)
            except (TypeError, ValueError):
                return None

        return {
            "id": row["id"],
            "title": row["title"],
            "source_type": row["source_type"],
            "source_ref": row["source_ref"],
            "status": row["status"],
            "stage_label": row["stage_label"],
            "progress": row["progress"],
            "error": row["error"],
            "options": load(row["options_json"]) or {},
            "paper_meta": load(row["paper_meta_json"]),
            "analysis": load(row["analysis_json"]),
            "script": load(row["script_json"]),
            "raw_text": row["raw_text"],
            "audio_path": row["audio_path"],
            "audio_duration_sec": row["audio_duration_sec"],
            "audio_bytes": row["audio_bytes"],
            "podcast_task_id": row["podcast_task_id"],
            "finished_round": row["finished_round"],
            "retry_count": row["retry_count"],
            "cover_path": row["cover_path"],
            "cover_width": row["cover_width"],
            "cover_height": row["cover_height"],
            "figures": load(row["figures_json"]) or [],
            "illustration": load(row["illustration_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ---------- CRUD ----------

    def create_episode(
        self,
        *,
        title: str,
        source_type: str,
        source_ref: str | None,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        now = utcnow()
        episode_id = new_id()
        self._execute(
            """
            INSERT INTO episodes
                (id, title, source_type, source_ref, status, stage_label, progress,
                 options_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'queued', '排队中', 0, ?, ?, ?)
            """,
            (
                episode_id,
                title,
                source_type,
                source_ref,
                json.dumps(options, ensure_ascii=False),
                now,
                now,
            ),
        )
        record = self.get_episode(episode_id)
        assert record is not None
        return record

    def get_episode(self, episode_id: str) -> dict[str, Any] | None:
        row = self._query_one("SELECT * FROM episodes WHERE id = ?", (episode_id,))
        return self._row_to_record(row) if row else None

    def list_episodes(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        status: str | None = None,
        q: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("status = ?")
            params.append(status)
        if q:
            where.append("title LIKE ?")
            params.append(f"%{q}%")
        clause = f"WHERE {' AND '.join(where)}" if where else ""

        total_row = self._query_one(
            f"SELECT COUNT(*) AS n FROM episodes {clause}", tuple(params)
        )
        total = int(total_row["n"]) if total_row else 0

        rows = self._query(
            f"SELECT * FROM episodes {clause} ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return [self._row_to_record(r) for r in rows], total

    def update_episode(self, episode_id: str, **fields: Any) -> None:
        """按字段名更新。JSON 字段传 dict/list，会自动序列化。"""
        json_fields = {
            "options",
            "paper_meta",
            "analysis",
            "script",
            "figures",
            "illustration",
        }
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            column = {
                "options": "options_json",
                "paper_meta": "paper_meta_json",
                "analysis": "analysis_json",
                "script": "script_json",
                "figures": "figures_json",
                "illustration": "illustration_json",
            }.get(key, key)
            if key in json_fields:
                value = json.dumps(value, ensure_ascii=False) if value is not None else None
            sets.append(f"{column} = ?")
            params.append(value)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.append(utcnow())
        params.append(episode_id)
        self._execute(f"UPDATE episodes SET {', '.join(sets)} WHERE id = ?", tuple(params))

    def delete_episode(self, episode_id: str) -> bool:
        cur = self._execute("DELETE FROM episodes WHERE id = ?", (episode_id,))
        return cur.rowcount > 0

    def count_by_status(self) -> dict[str, int]:
        rows = self._query("SELECT status, COUNT(*) AS n FROM episodes GROUP BY status")
        return {r["status"]: int(r["n"]) for r in rows}
