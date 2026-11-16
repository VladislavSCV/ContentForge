"""Content Database (замена Airtable/Supabase): SQLite, статусы new ➔ generating ➔ review ➔ published."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from .config import settings
from .schemas import ContentPackage

NEW, GENERATING, REVIEW, PUBLISHING, PUBLISHED, FAILED = "new", "generating", "review", "publishing", "published", "failed"
SCHEDULED = "scheduled"  # в очереди: уйдёт сам в назначенное окно

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    status           TEXT NOT NULL,
    admin_chat_id    INTEGER NOT NULL,
    source_url       TEXT,
    source_text      TEXT NOT NULL,
    package_json     TEXT,
    tg_html          TEXT,
    video_path       TEXT,
    tg_message_id    INTEGER,
    youtube_video_id TEXT,
    error            TEXT,
    cta_mode         TEXT,
    scheduled_at     TEXT,
    linkedin_json    TEXT,
    credit           TEXT,
    yt_scheduled_at  TEXT
);
CREATE TABLE IF NOT EXISTS topics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    url        TEXT NOT NULL UNIQUE,
    title      TEXT NOT NULL,
    summary    TEXT NOT NULL,
    source     TEXT NOT NULL,
    score      REAL NOT NULL,
    status     TEXT NOT NULL  -- offered | used | skipped
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- просмотры и пересылки поста в канале: Bot API их не отдаёт, автор вводит руками (/tgstats)
CREATE TABLE IF NOT EXISTS tg_stats (
    post_id    INTEGER PRIMARY KEY,
    views      INTEGER NOT NULL,
    forwards   INTEGER NOT NULL,
    reactions  INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
-- выгрузка «Post performance» из LinkedIn: по строке на пост, повторная загрузка обновляет цифры
CREATE TABLE IF NOT EXISTS linkedin_stats (
    url           TEXT PRIMARY KEY,
    published     TEXT NOT NULL,   -- YYYY-MM-DD
    impressions   INTEGER NOT NULL,
    reached       INTEGER NOT NULL,
    followers     INTEGER NOT NULL,
    profile_views INTEGER NOT NULL,
    reactions     INTEGER NOT NULL,
    comments      INTEGER NOT NULL,
    reposts       INTEGER NOT NULL,
    saves         INTEGER NOT NULL,
    sends         INTEGER NOT NULL,
    imported_at   TEXT NOT NULL
);
"""

# колонки, добавленные после первых запусков: в готовой базе их нет
_MIGRATIONS = ["ALTER TABLE posts ADD COLUMN cta_mode TEXT", "ALTER TABLE posts ADD COLUMN scheduled_at TEXT",
               "ALTER TABLE posts ADD COLUMN linkedin_json TEXT",
               "ALTER TABLE posts ADD COLUMN credit TEXT",
               "ALTER TABLE posts ADD COLUMN yt_scheduled_at TEXT",
               "ALTER TABLE posts ADD COLUMN opinion TEXT"]


@dataclass
class Post:
    id: int
    created_at: str
    updated_at: str
    status: str
    admin_chat_id: int
    source_url: str | None
    source_text: str
    package_json: str | None
    tg_html: str | None
    video_path: str | None
    tg_message_id: int | None
    youtube_video_id: str | None
    error: str | None
    cta_mode: str | None = None
    scheduled_at: str | None = None
    linkedin_json: str | None = None   # {"text", "first_comment"} — черновик поста для LinkedIn
    credit: str | None = None          # «сайт\nссылка» — источник темы из ленты; строка «Источник» в посте и описании
    yt_scheduled_at: str | None = None  # когда уйдёт на YouTube; scheduled_at — когда в Telegram
    opinion: str | None = None         # позиция автора в одну фразу: без неё пост — пересказ чужого, как у всех

    @property
    def package(self) -> ContentPackage | None:
        return ContentPackage.model_validate_json(self.package_json) if self.package_json else None

    @property
    def video(self) -> Path | None:
        p = Path(self.video_path) if self.video_path else None
        return p if p and p.exists() else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def init() -> list[int]:
    """Создаёт базу. Возвращает id постов, которые прервал перезапуск (их стоит показать автору)."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(settings.db_path) as db:
        await db.executescript(_SCHEMA)
        for sql in _MIGRATIONS:
            try:
                await db.execute(sql)
            except aiosqlite.OperationalError:
                pass  # колонка уже есть — база новее схемы, с которой её создавали
        # всё, что «висело» в генерации при рестарте, помечаем как упавшее
        async with db.execute("SELECT id FROM posts WHERE status IN (?, ?)", (GENERATING, PUBLISHING)) as cur:
            interrupted = [r[0] for r in await cur.fetchall()]
        await db.execute(
            "UPDATE posts SET status=?, error=? WHERE status IN (?, ?)",
            (FAILED, "прервано перезапуском", GENERATING, PUBLISHING),
        )
        await db.commit()
    return interrupted


async def create(admin_chat_id: int, source_text: str, source_url: str | None) -> int:
    async with aiosqlite.connect(settings.db_path) as db:
        cur = await db.execute(
            "INSERT INTO posts (created_at, updated_at, status, admin_chat_id, source_url, source_text) VALUES (?,?,?,?,?,?)",
            (_now(), _now(), NEW, admin_chat_id, source_url, source_text),
        )
        await db.commit()
        return cur.lastrowid


async def get(post_id: int) -> Post | None:
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM posts WHERE id=?", (post_id,)) as cur:
            row = await cur.fetchone()
    return Post(**dict(row)) if row else None


async def latest(limit: int = 10) -> list[Post]:
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,)) as cur:
            rows = await cur.fetchall()
    return [Post(**dict(r)) for r in rows]


async def update(post_id: int, **fields) -> None:
    if "package" in fields:
        pkg: ContentPackage = fields.pop("package")
        fields["package_json"] = json.dumps(pkg.model_dump(), ensure_ascii=False)
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute(f"UPDATE posts SET {cols} WHERE id=?", (*fields.values(), post_id))
        await db.commit()


# ---------- очередь публикаций ----------

async def due_posts(now_iso: str) -> list[Post]:
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM posts WHERE status=? AND (scheduled_at<=? OR yt_scheduled_at<=?) "
                              "ORDER BY COALESCE(scheduled_at, yt_scheduled_at)", (SCHEDULED, now_iso, now_iso)) as cur:
            rows = await cur.fetchall()
    return [Post(**dict(r)) for r in rows]


async def queued_posts() -> list[Post]:
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM posts WHERE status=? ORDER BY COALESCE(scheduled_at, yt_scheduled_at)",
                              (SCHEDULED,)) as cur:
            rows = await cur.fetchall()
    return [Post(**dict(r)) for r in rows]


# ---------- темы ----------

@dataclass
class Topic:
    id: int
    created_at: str
    url: str
    title: str
    summary: str
    source: str
    score: float
    status: str


async def topic_urls() -> set[str]:
    async with aiosqlite.connect(settings.db_path) as db:
        async with db.execute("SELECT url FROM topics") as cur:
            return {r[0] for r in await cur.fetchall()}


async def add_topic(url: str, title: str, summary: str, source: str, score: float) -> Topic | None:
    """Новая тема со статусом offered; None, если такой адрес уже предлагали."""
    async with aiosqlite.connect(settings.db_path) as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO topics (created_at, url, title, summary, source, score, status) VALUES (?,?,?,?,?,?,?)",
            (_now(), url, title, summary, source, score, "offered"))
        await db.commit()
    if not cur.rowcount:
        return None
    return await get_topic(cur.lastrowid)


async def get_topic(topic_id: int) -> Topic | None:
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM topics WHERE id=?", (topic_id,)) as cur:
            row = await cur.fetchone()
    return Topic(**dict(row)) if row else None


async def set_topic_status(topic_id: int, status: str) -> None:
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute("UPDATE topics SET status=? WHERE id=?", (status, topic_id))
        await db.commit()


# ---------- служебное состояние (когда в последний раз присылали темы) ----------

async def get_state(key: str) -> str | None:
    async with aiosqlite.connect(settings.db_path) as db:
        async with db.execute("SELECT value FROM state WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def set_state(key: str, value: str) -> None:
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute("INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, value))
        await db.commit()


# ---------- Telegram: цифры, которые автор вводит руками ----------

async def set_tg_stats(post_id: int, views: int, forwards: int, reactions: int = 0) -> None:
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute(
            "INSERT INTO tg_stats (post_id, views, forwards, reactions, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(post_id) DO UPDATE SET views=excluded.views, forwards=excluded.forwards, "
            "reactions=excluded.reactions, updated_at=excluded.updated_at", (post_id, views, forwards, reactions, _now()))
        await db.commit()


async def tg_stats() -> dict[int, dict]:
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tg_stats") as cur:
            return {r["post_id"]: dict(r) for r in await cur.fetchall()}


# ---------- LinkedIn: выгрузка статистики ----------

_LI_COLS = ("url", "published", "impressions", "reached", "followers", "profile_views",
            "reactions", "comments", "reposts", "saves", "sends")


async def upsert_linkedin_stats(rows: list[dict]) -> None:
    async with aiosqlite.connect(settings.db_path) as db:
        for r in rows:
            await db.execute(
                f"INSERT OR REPLACE INTO linkedin_stats ({', '.join(_LI_COLS)}, imported_at) "
                f"VALUES ({', '.join('?' * len(_LI_COLS))}, ?)", (*(r[c] for c in _LI_COLS), _now()))
        await db.commit()


async def linkedin_stats() -> list[dict]:
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM linkedin_stats ORDER BY published DESC") as cur:
            return [dict(r) for r in await cur.fetchall()]
