"""Канонический слой БД КӨЗ (§0.2) — подписи с `conn` ПЕРВЫМ аргументом.

4 таблицы: posts / extracted / scores / audit.
row_factory = sqlite3.Row. Соединение из lifespan (роуты) или своё в standalone.
"""

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from app import config
from app.models import Entity, Extracted, Post, Score, VisualConcept

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    platform TEXT,
    author_handle TEXT,
    url TEXT,
    caption TEXT,
    posted_at TEXT,
    media_path TEXT,
    thumb_url TEXT,
    source TEXT,
    revealed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS extracted (
    post_id TEXT PRIMARY KEY,
    caption TEXT,
    transcript TEXT,
    ocr_text TEXT,
    visual_concepts_json TEXT,
    combined_text TEXT,
    entities_json TEXT
);
CREATE TABLE IF NOT EXISTS scores (
    post_id TEXT PRIMARY KEY,
    risk INTEGER,
    category TEXT,
    class_probs_json TEXT,
    top_features_json TEXT,
    recommended_action TEXT,
    scored_at TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    post_id TEXT,
    action TEXT,
    actor TEXT,
    detail TEXT
);
"""

_POST_COLS = (
    "id", "platform", "author_handle", "url", "caption",
    "posted_at", "media_path", "thumb_url", "source",
)


def connect(path: "str | Path | None" = None) -> sqlite3.Connection:
    """Открыть соединение. path=None -> config.DB_PATH. row_factory=sqlite3.Row."""
    db_path = str(path) if path is not None else str(config.DB_PATH)
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Создать все таблицы (идемпотентно)."""
    conn.executescript(SCHEMA)
    conn.commit()


def _post_from_row(r: sqlite3.Row) -> Post:
    return Post(*[r[k] for k in _POST_COLS])


def insert_post(conn: sqlite3.Connection, post: Post) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO posts "
        "(id, platform, author_handle, url, caption, posted_at, media_path, thumb_url, source) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (post.id, post.platform, post.author_handle, post.url, post.caption,
         post.posted_at, post.media_path, post.thumb_url, post.source),
    )
    conn.commit()


def get_post(conn: sqlite3.Connection, post_id: str) -> "Post | None":
    r = conn.execute(
        "SELECT id, platform, author_handle, url, caption, posted_at, "
        "media_path, thumb_url, source FROM posts WHERE id=?",
        (post_id,),
    ).fetchone()
    return _post_from_row(r) if r is not None else None


def get_revealed_posts(conn: sqlite3.Connection) -> list[Post]:
    rows = conn.execute(
        "SELECT id, platform, author_handle, url, caption, posted_at, "
        "media_path, thumb_url, source FROM posts WHERE revealed=1 ORDER BY posted_at"
    ).fetchall()
    return [_post_from_row(r) for r in rows]


def upsert_extracted(conn: sqlite3.Connection, ex: Extracted) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO extracted "
        "(post_id, caption, transcript, ocr_text, visual_concepts_json, combined_text, entities_json) "
        "VALUES (?,?,?,?,?,?,?)",
        (ex.post_id, ex.caption, ex.transcript, ex.ocr_text,
         json.dumps([asdict(v) for v in ex.visual_concepts], ensure_ascii=False),
         ex.combined_text,
         json.dumps([asdict(e) for e in ex.entities], ensure_ascii=False)),
    )
    conn.commit()


def get_extracted(conn: sqlite3.Connection, post_id: str) -> "Extracted | None":
    r = conn.execute(
        "SELECT post_id, caption, transcript, ocr_text, visual_concepts_json, "
        "combined_text, entities_json FROM extracted WHERE post_id=?",
        (post_id,),
    ).fetchone()
    if r is None:
        return None
    vcs = [VisualConcept(**v) for v in json.loads(r["visual_concepts_json"] or "[]")]
    ents = [Entity(**e) for e in json.loads(r["entities_json"] or "[]")]
    return Extracted(
        post_id=r["post_id"], caption=r["caption"], transcript=r["transcript"],
        ocr_text=r["ocr_text"], visual_concepts=vcs,
        combined_text=r["combined_text"], entities=ents,
    )


def upsert_score(conn: sqlite3.Connection, score: Score,
                 recommended_action: str, scored_at: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO scores "
        "(post_id, risk, category, class_probs_json, top_features_json, recommended_action, scored_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (score.post_id, score.risk, score.category,
         json.dumps(score.class_probs, ensure_ascii=False),
         json.dumps([asdict(f) for f in score.top_features], ensure_ascii=False),
         recommended_action, scored_at),
    )
    conn.commit()


def get_score_row(conn: sqlite3.Connection, post_id: str) -> "sqlite3.Row | None":
    return conn.execute("SELECT * FROM scores WHERE post_id=?", (post_id,)).fetchone()


def add_audit(conn: sqlite3.Connection, ts: str, post_id: str,
              action: str, actor: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO audit (ts, post_id, action, actor, detail) VALUES (?,?,?,?,?)",
        (ts, post_id, action, actor, detail),
    )
    conn.commit()


def reveal_next(conn: sqlite3.Connection, n: int) -> int:
    """Раскрыть следующие n постов (revealed=1), вернуть число раскрытых."""
    rows = conn.execute(
        "SELECT id FROM posts WHERE revealed=0 ORDER BY posted_at LIMIT ?", (n,)
    ).fetchall()
    ids = [r["id"] for r in rows]
    for pid in ids:
        conn.execute("UPDATE posts SET revealed=1 WHERE id=?", (pid,))
    conn.commit()
    return len(ids)


def reveal_post(conn: sqlite3.Connection, post_id: str) -> None:
    """Раскрыть один конкретный пост (для F8 — добавленные telegram-посты)."""
    conn.execute("UPDATE posts SET revealed=1 WHERE id=?", (post_id,))
    conn.commit()
