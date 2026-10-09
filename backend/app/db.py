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

# ---------------------------------------------------------------------------
# 列表排序（契约 §2.5 `sort` 参数）
# ---------------------------------------------------------------------------
#
# 值是**直接拼进 SQL 的**，所以这里必须是一张封闭的白名单：新加排序方式时
# 只能往这张表里加，绝不允许把请求里的字符串直接交给 SQL。
#
# 每一档都补一个 `rowid` 作为最后的 tie-breaker：同一秒里建的两集
# （批量导入时很常见）若没有稳定的次级键，翻页会出现「同一条出现两次、
# 另一条再也不出现」的经典分页错位。
EPISODE_ORDER: dict[str, str] = {
    "created_desc": "created_at DESC, rowid DESC",
    "created_asc": "created_at ASC, rowid ASC",
    "updated_desc": "updated_at DESC, rowid DESC",
    # 标题按中文/英文混排，用 NOCASE 让英文大小写不敏感；CJK 没有大小写概念，
    # 不受影响。不用 COLLATE 的话「Apple」会排到所有小写字母后面。
    "title_asc": "title COLLATE NOCASE ASC, rowid DESC",
    # `IS NULL` 参与排序：SQLite 里 NULL 默认排在最小值位置，
    # 直接 DESC 会让「没有音频的失败/进行中任务」全冒到最前面。
    "duration_desc": "audio_duration_sec IS NULL, audio_duration_sec DESC, rowid DESC",
}

DEFAULT_EPISODE_SORT = "created_desc"

# 终态 / 伪状态：契约 §2.5 里 `status=running` 表示「还没跑完的那些」。
# 把它放在存储层是因为「哪些算跑完」是数据定义，搜索条件、统计、清理
# 迟早都要用同一份定义，写两遍就会有一处忘了改。
TERMINAL_STATUSES = frozenset({"completed", "failed"})
RUNNING_STATUS = "running"

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
    timings_json       TEXT,
    video_path         TEXT,
    video_json         TEXT,
    video_landscape_json TEXT,
    versions_json      TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episodes_created ON episodes(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_status  ON episodes(status);

-- ---------------------------------------------------------------- V2：账号

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS albums (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    title       TEXT NOT NULL,
    description TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_albums_user ON albums(user_id);
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
            "timings_json": "TEXT",
            "video_path": "TEXT",
            "video_json": "TEXT",
            "video_landscape_json": "TEXT",
            "cover_width": "INTEGER",
            "cover_height": "INTEGER",
            "figures_json": "TEXT",
            "illustration_json": "TEXT",
            "versions_json": "TEXT",
            # ---- V2：归属 / 可见性 / 专辑 ----
            # 都允许 NULL：V1 时代的单集没有归属，第一个注册的用户会认领它们
            "user_id": "TEXT",
            "visibility": "TEXT",
            "share_token": "TEXT",
            "album_id": "TEXT",
        }
        for column, sql_type in additions.items():
            if column not in existing:
                self._conn.execute(f"ALTER TABLE episodes ADD COLUMN {column} {sql_type}")

        # V2 的 visibility 允许 NULL（老数据没有这个字段），但 NULL 在 SQL 里
        # 不参与 `= 'public'` 之外的任何比较 —— 留着它会养出一类
        # 「为什么这个 WHERE 不生效」的怪 bug。这里一次性归一化成 'private'。
        self._conn.execute(
            "UPDATE episodes SET visibility = 'private' WHERE visibility IS NULL"
        )

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
            "timings": load(row["timings_json"]) or [],
            "video_path": row["video_path"],
            "video": load(row["video_json"]),
            "video_landscape": load(row["video_landscape_json"]),
            "versions": load(row["versions_json"]) or {},
            "user_id": row["user_id"],
            "visibility": row["visibility"] or "private",
            "share_token": row["share_token"],
            "album_id": row["album_id"],
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
        user_id: str | None = None,
    ) -> dict[str, Any]:
        now = utcnow()
        episode_id = new_id()
        self._execute(
            """
            INSERT INTO episodes
                (id, title, source_type, source_ref, status, stage_label, progress,
                 options_json, user_id, visibility, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'queued', '排队中', 0, ?, ?, 'private', ?, ?)
            """,
            (
                episode_id,
                title,
                source_type,
                source_ref,
                json.dumps(options, ensure_ascii=False),
                user_id,
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
        user_id: str | None = None,
        album_id: str | None = None,
        unassigned_only: bool = False,
        sort: str = DEFAULT_EPISODE_SORT,
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        params: list[Any] = []
        if status == RUNNING_STATUS:
            # 聚合筛选：用户脑子里的分类是「在跑的 / 完成的 / 失败的」，
            # 不是「正在合成音频的」。让他为了「看看还在跑什么」去点五个
            # 状态各看一遍，是把这个心智负担推给了用户。
            where.append(f"status NOT IN ({', '.join('?' * len(TERMINAL_STATUSES))})")
            params.extend(sorted(TERMINAL_STATUSES))
        elif status:
            where.append("status = ?")
            params.append(status)
        if q:
            where.append("title LIKE ?")
            params.append(f"%{q}%")
        if user_id is not None:
            where.append("user_id = ?")
            params.append(user_id)
        if album_id is not None:
            where.append("album_id = ?")
            params.append(album_id)
        elif unassigned_only:
            where.append("album_id IS NULL")
        clause = f"WHERE {' AND '.join(where)}" if where else ""

        total_row = self._query_one(
            f"SELECT COUNT(*) AS n FROM episodes {clause}", tuple(params)
        )
        total = int(total_row["n"]) if total_row else 0

        rows = self._query(
            f"SELECT * FROM episodes {clause} ORDER BY {EPISODE_ORDER[sort]} LIMIT ? OFFSET ?",
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
            "timings",
            "video",
            "video_landscape",
            "versions",
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
                "timings": "timings_json",
                "video": "video_json",
                "video_landscape": "video_landscape_json",
                "versions": "versions_json",
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

    # ---------- V2：用户 / 会话 / 专辑 / 分享 ----------

    # ---------- 用户 ----------

    def count_users(self) -> int:
        row = self._query_one("SELECT COUNT(*) AS n FROM users")
        return int(row["n"]) if row else 0

    def is_open_mode(self) -> bool:
        """库里一个用户都没有 = 开放模式（不需要登录）。

        每次请求都查一次 COUNT：SQLite 上这是微秒级，而且省掉一个必须手动维护的
        缓存失效逻辑 —— 那个才是真正容易出错的地方。
        """
        return self.count_users() == 0

    def create_user(
        self, *, user_id: str, username: str, password_hash: str, display_name: str = ""
    ) -> dict[str, Any]:
        now = utcnow()
        self._execute(
            """
            INSERT INTO users (id, username, display_name, password_hash, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, username, display_name, password_hash, now),
        )
        user = self.get_user(user_id)
        assert user is not None
        return user

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        row = self._query_one(_USER_COLUMNS + " WHERE id = ?", (user_id,))
        return dict(row) if row else None

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        row = self._query_one(_USER_COLUMNS + " WHERE username = ?", (username,))
        return dict(row) if row else None

    def update_user_password(self, user_id: str, password_hash: str) -> None:
        self._execute(
            "UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id)
        )

    def claim_orphan_episodes(self, user_id: str) -> int:
        """把无主单集认领给某个用户。返回认领了几集。

        第一个注册的用户调用它。没有这一步，V1 时代攒下的数据在上 V2 之后
        会变成「谁都看不到」的孤儿 —— 数据还在磁盘上，但接口永远不返回。
        """
        cur = self._execute(
            "UPDATE episodes SET user_id = ?, updated_at = ? WHERE user_id IS NULL",
            (user_id, utcnow()),
        )
        return cur.rowcount or 0

    def count_episodes_since(self, user_id: str, since_iso: str) -> int:
        """这个账号在 `since_iso` 之后建了多少集（用于每日配额）。

        `created_at` 统一是 `utcnow()` 写进去的 ISO 串（带 +00:00），
        同格式下按字符串比较等价于按时间比较，所以这里不用 SQLite 的日期函数 ——
        那些函数对带时区偏移的串处理得并不一致。
        """
        row = self._query_one(
            "SELECT COUNT(*) AS n FROM episodes WHERE user_id = ? AND created_at >= ?",
            (user_id, since_iso),
        )
        return int(row["n"]) if row else 0

    # ---------- 会话 ----------

    def create_session(self, *, token: str, user_id: str, expires_at: str) -> None:
        self._execute(
            """
            INSERT INTO sessions (token, user_id, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (token, user_id, utcnow(), expires_at),
        )

    def user_for_session(self, token: str) -> dict[str, Any] | None:
        """按 token 取用户。过期的会话顺手删掉（省一个定时任务）。"""
        row = self._query_one("SELECT * FROM sessions WHERE token = ?", (token,))
        if not row:
            return None
        if _session_expired(row["expires_at"]):
            self.delete_session(token)
            return None
        user_row = self._query_one(_USER_COLUMNS + " WHERE id = ?", (row["user_id"],))
        return dict(user_row) if user_row else None

    def delete_session(self, token: str) -> None:
        self._execute("DELETE FROM sessions WHERE token = ?", (token,))

    def delete_sessions_for_user(self, user_id: str) -> None:
        self._execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def delete_other_sessions(self, user_id: str, keep_token: str | None) -> None:
        """删掉该用户除 `keep_token` 之外的所有会话。

        改口令时用。不能写成「先全删再补回当前这条」—— 那样一旦补回那步漏了，
        用户改完口令自己就被踢出去了（第一版就是这么写的，被测试逮住）。
        """
        if keep_token:
            self._execute(
                "DELETE FROM sessions WHERE user_id = ? AND token != ?",
                (user_id, keep_token),
            )
        else:
            self._execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def purge_expired_sessions(self) -> int:
        cur = self._execute("DELETE FROM sessions WHERE expires_at <= ?", (utcnow(),))
        return cur.rowcount or 0

    # ---------- 专辑 ----------

    def create_album(
        self, *, album_id: str, user_id: str, title: str, description: str | None = None
    ) -> dict[str, Any]:
        now = utcnow()
        self._execute(
            """
            INSERT INTO albums (id, user_id, title, description, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (album_id, user_id, title, description, now, now),
        )
        album = self.get_album(album_id)
        assert album is not None
        return album

    def get_album(self, album_id: str, *, user_id: str | None = None) -> dict[str, Any] | None:
        """按 id 取专辑。给了 user_id 就一并校验归属 —— 越权返回 None（上层转 404）。"""
        if user_id is None:
            row = self._query_one(_ALBUM_COLUMNS + " WHERE id = ?", (album_id,))
        else:
            row = self._query_one(
                _ALBUM_COLUMNS + " WHERE id = ? AND user_id = ?", (album_id, user_id)
            )
        return dict(row) if row else None

    def list_albums(self, user_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            _ALBUM_COLUMNS + " WHERE user_id = ? ORDER BY updated_at DESC, rowid DESC",
            (user_id,),
        )
        return [dict(r) for r in rows]

    def update_album(self, album_id: str, *, user_id: str, **fields: Any) -> None:
        allowed = {"title", "description"}
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key} = ?")
            params.append(value)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.append(utcnow())
        params.extend([album_id, user_id])
        self._execute(
            f"UPDATE albums SET {', '.join(sets)} WHERE id = ? AND user_id = ?", tuple(params)
        )

    def touch_album(self, album_id: str) -> None:
        self._execute("UPDATE albums SET updated_at = ? WHERE id = ?", (utcnow(), album_id))

    def delete_album(self, album_id: str, *, user_id: str) -> bool:
        """删专辑，但**不删单集** —— 只把它们的 album_id 置空。

        专辑是「分组」不是「容器」：删掉一个合集不该把里面的播客一起删了。
        """
        album = self.get_album(album_id, user_id=user_id)
        if not album:
            return False
        self._execute(
            "UPDATE episodes SET album_id = NULL WHERE album_id = ? AND user_id = ?",
            (album_id, user_id),
        )
        self._execute("DELETE FROM albums WHERE id = ? AND user_id = ?", (album_id, user_id))
        return True

    def count_episodes_in_album(self, album_id: str) -> int:
        row = self._query_one(
            "SELECT COUNT(*) AS n FROM episodes WHERE album_id = ?", (album_id,)
        )
        return int(row["n"]) if row else 0

    def assign_episodes_to_album(
        self, album_id: str, episode_ids: list[str], *, user_id: str
    ) -> int:
        """把若干单集加入专辑。只动属于该用户的单集（越权的静默跳过，调用方已校验）。"""
        if not episode_ids:
            return 0
        placeholders = ", ".join("?" for _ in episode_ids)
        cur = self._execute(
            f"""
            UPDATE episodes SET album_id = ?, updated_at = ?
            WHERE id IN ({placeholders}) AND user_id = ?
            """,
            (album_id, utcnow(), *episode_ids, user_id),
        )
        return cur.rowcount or 0

    def remove_episode_from_album(self, album_id: str, episode_id: str, *, user_id: str) -> int:
        cur = self._execute(
            """
            UPDATE episodes SET album_id = NULL, updated_at = ?
            WHERE id = ? AND album_id = ? AND user_id = ?
            """,
            (utcnow(), episode_id, album_id, user_id),
        )
        return cur.rowcount or 0

    # ---------- 分享 ----------

    def query_public_episodes(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """公开分享出去的单集，最新在前。首页在未登录时用它挑展示内容。"""
        rows = self._query(
            """
            SELECT * FROM episodes
            WHERE visibility = 'public' AND share_token IS NOT NULL AND status = 'completed'
            ORDER BY created_at DESC, rowid DESC LIMIT ?
            """,
            (limit,),
        )
        return [self._row_to_record(r) for r in rows]

    def episode_by_share_token(self, token: str) -> dict[str, Any] | None:
        """按分享 token 取单集。**只有 visibility=public 才算** ——
        私有化的那一瞬间旧链接就该失效，不能靠「token 还在」放行。"""
        row = self._query_one(
            "SELECT * FROM episodes WHERE share_token = ? AND visibility = 'public'", (token,)
        )
        return self._row_to_record(row) if row else None

    def count_by_status(self) -> dict[str, int]:
        rows = self._query("SELECT status, COUNT(*) AS n FROM episodes GROUP BY status")
        return {r["status"]: int(r["n"]) for r in rows}


# --------------------------------------------------------------------------
# V2：用户 / 会话 / 专辑
#
# 刻意写在同一个 Database 类里（而不是拆成第二个类）：这个项目的存储层只有
# 一个 SQLite 连接和一把锁，拆开之后「哪些操作在同一把锁下」反而看不清。
# --------------------------------------------------------------------------


def _session_expired(value: str | None) -> bool:
    from datetime import datetime, timezone

    if not value:
        return True
    try:
        expires = datetime.fromisoformat(value)
    except ValueError:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= datetime.now(timezone.utc)


_USER_COLUMNS = "SELECT id, username, display_name, password_hash, created_at FROM users"
_ALBUM_COLUMNS = (
    "SELECT id, user_id, title, description, created_at, updated_at FROM albums"
)
