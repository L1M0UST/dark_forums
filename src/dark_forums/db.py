from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


@dataclass(frozen=True)
class PostRecord:
    url: str
    title: str
    created_at: str
    author_name: str
    author_posts: str | None
    author_threads: str | None
    author_joined: str | None
    author_reputation: str | None
    author_contacts: str | None
    scraped_at: str
    first_post_text: str
    download_urls_json: str
    screenshot_path: str | None


@dataclass(frozen=True)
class ThreadRecord:
    url: str
    discovered_at: str
    status: str
    last_error: str | None


def open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS threads (
            url TEXT PRIMARY KEY,
            discovered_at TEXT NOT NULL,
            status TEXT NOT NULL,
            last_error TEXT,
            failure_count INTEGER NOT NULL DEFAULT 0,
            last_fetched_at TEXT,
            content_path TEXT,
            extracted_at TEXT,
            downloads_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    _migrate_threads(conn)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cursors (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    conn.execute("DROP TABLE IF EXISTS downloads")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_threads_status ON threads(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_threads_extracted_at ON threads(extracted_at)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS posts (
            url TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            author_name TEXT NOT NULL,
            author_posts TEXT,
            author_threads TEXT,
            author_joined TEXT,
            author_reputation TEXT,
            author_contacts TEXT,
            scraped_at TEXT NOT NULL,
            first_post_text TEXT NOT NULL,
            download_urls_json TEXT NOT NULL,
            screenshot_path TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS deliveries (
            post_url TEXT NOT NULL,
            provider TEXT NOT NULL,
            delivered_at TEXT NOT NULL,
            message_id TEXT,
            PRIMARY KEY (post_url, provider)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS replies (
            thread_url TEXT PRIMARY KEY,
            replied_at TEXT NOT NULL
        )
        """
    )
    _migrate_posts(conn)
    _migrate_deliveries(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_scraped_at ON posts(scraped_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_deliveries_provider ON deliveries(provider)")
    conn.commit()


def _migrate_posts(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA table_info(posts)")
    existing = {str(r[1]) for r in cur.fetchall()}
    if "title_screenshot_path" in existing:
        conn.execute("DROP TABLE IF EXISTS posts__new")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS posts__new (
                url TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                author_name TEXT NOT NULL,
                author_posts TEXT,
                author_threads TEXT,
                author_joined TEXT,
                author_reputation TEXT,
                author_contacts TEXT,
                scraped_at TEXT NOT NULL,
                first_post_text TEXT NOT NULL,
                download_urls_json TEXT NOT NULL,
                screenshot_path TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO posts__new(
                url, title, created_at, author_name, author_posts, author_threads, author_joined,
                author_reputation, author_contacts, scraped_at, first_post_text, download_urls_json, screenshot_path
            )
            SELECT
                url, title, created_at, author_name, author_posts, author_threads, author_joined,
                author_reputation, author_contacts, scraped_at, first_post_text, download_urls_json, screenshot_path
            FROM posts
            """
        )
        conn.execute("DROP TABLE posts")
        conn.execute("ALTER TABLE posts__new RENAME TO posts")
        conn.commit()
        cur = conn.execute("PRAGMA table_info(posts)")
        existing = {str(r[1]) for r in cur.fetchall()}
    if "screenshot_path" not in existing:
        conn.execute("ALTER TABLE posts ADD COLUMN screenshot_path TEXT")
    conn.commit()


def _migrate_deliveries(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA table_info(deliveries)")
    cols = [str(r[1]) for r in cur.fetchall()]
    if not cols:
        return

    # legacy schema used post_url as the only primary key.
    if "provider" not in set(cols):
        conn.execute("DROP TABLE deliveries")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deliveries (
                post_url TEXT NOT NULL,
                provider TEXT NOT NULL,
                delivered_at TEXT NOT NULL,
                message_id TEXT,
                PRIMARY KEY (post_url, provider)
            )
            """
        )
        conn.commit()
        return

    idx_rows = conn.execute("PRAGMA index_list(deliveries)").fetchall()
    pk_ok = False
    for r in idx_rows:
        # columns: seq, name, unique, origin, partial
        if len(r) >= 4 and str(r[3]) == "pk":
            idx_name = str(r[1])
            info = conn.execute(f"PRAGMA index_info({idx_name})").fetchall()
            pk_cols = [str(x[2]) for x in info]
            if pk_cols == ["post_url", "provider"]:
                pk_ok = True
            break

    if pk_ok:
        return


def has_reply(conn: sqlite3.Connection, url: str) -> bool:
    try:
        row = conn.execute("SELECT 1 FROM replies WHERE thread_url=? LIMIT 1", (url,)).fetchone()
        return row is not None
    except Exception:
        return False


def mark_replied(conn: sqlite3.Connection, url: str, replied_at: str) -> None:
    conn.execute(
        """
        INSERT INTO replies(thread_url, replied_at)
        VALUES(?, ?)
        ON CONFLICT(thread_url) DO UPDATE SET replied_at=excluded.replied_at
        """,
        (url, replied_at),
    )
    conn.commit()

    conn.execute("DROP TABLE IF EXISTS deliveries__new")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS deliveries__new (
            post_url TEXT NOT NULL,
            provider TEXT NOT NULL,
            delivered_at TEXT NOT NULL,
            message_id TEXT,
            PRIMARY KEY (post_url, provider)
        )
        """
    )
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO deliveries__new(post_url, provider, delivered_at, message_id)
            SELECT post_url, provider, delivered_at, message_id FROM deliveries
            """
        )
    except Exception:
        pass
    conn.execute("DROP TABLE deliveries")
    conn.execute("ALTER TABLE deliveries__new RENAME TO deliveries")
    conn.commit()


def get_cursor(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM cursors WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    v = row[0]
    return (str(v) if v is not None else None)


def set_cursor(conn: sqlite3.Connection, key: str, value: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO cursors(key, value, updated_at)
        VALUES(?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (key, value, now),
    )
    conn.commit()


def prune_threads(conn: sqlite3.Connection, retention_days: int = 30) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(retention_days))
    cutoff_iso = cutoff.isoformat()
    before = conn.total_changes
    conn.execute(
        """
        DELETE FROM threads
        WHERE status IN ('done','failed')
          AND COALESCE(extracted_at, last_fetched_at, discovered_at) < ?
        """,
        (cutoff_iso,),
    )
    conn.commit()
    return conn.total_changes - before


def canonicalize_thread_url(url: str) -> str:
    parsed = urlparse(url)
    q = parsed.query
    if q:
        existing = dict(parse_qsl(q, keep_blank_values=True))
        for k in (
            "action",
            "page",
            "pid",
            "tid",
            "highlight",
            "session",
            "utm_source",
            "utm_medium",
            "utm_campaign",
        ):
            if k in existing:
                existing.pop(k, None)
        q = urlencode(list(existing.items())) if existing else ""
    return urlunparse(parsed._replace(fragment="", query=q))


def migrate_thread_url(conn: sqlite3.Connection, old_url: str, new_url: str) -> None:
    if old_url == new_url:
        return

    row = conn.execute(
        "SELECT discovered_at, status, last_error, last_fetched_at, content_path, extracted_at, downloads_count FROM threads WHERE url=?",
        (old_url,),
    ).fetchone()
    if not row:
        return

    exists = conn.execute("SELECT 1 FROM threads WHERE url=?", (new_url,)).fetchone() is not None
    if not exists:
        conn.execute(
            """
            INSERT INTO threads(url, discovered_at, status, last_error, last_fetched_at, content_path, extracted_at, downloads_count)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_url,
                row[0],
                row[1],
                row[2],
                row[3],
                row[4],
                row[5],
                row[6],
            ),
        )

    conn.execute("DELETE FROM threads WHERE url=?", (old_url,))
    conn.commit()


def has_post(conn: sqlite3.Connection, url: str) -> bool:
    cur = conn.execute("SELECT 1 FROM posts WHERE url=? LIMIT 1", (url,))
    return cur.fetchone() is not None


def insert_post(conn: sqlite3.Connection, rec: PostRecord) -> None:
    conn.execute(
        """
        INSERT INTO posts(
            url, title, created_at, author_name, author_posts, author_threads, author_joined,
            author_reputation, author_contacts, scraped_at, first_post_text, download_urls_json, screenshot_path
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rec.url,
            rec.title,
            rec.created_at,
            rec.author_name,
            rec.author_posts,
            rec.author_threads,
            rec.author_joined,
            rec.author_reputation,
            rec.author_contacts,
            rec.scraped_at,
            rec.first_post_text,
            rec.download_urls_json,
            rec.screenshot_path,
        ),
    )
    conn.commit()


def has_delivery(conn: sqlite3.Connection, post_url: str, provider: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM deliveries WHERE post_url=? AND provider=? LIMIT 1",
        (post_url, provider),
    ).fetchone()
    return row is not None


def mark_delivered(
    conn: sqlite3.Connection,
    post_url: str,
    provider: str,
    delivered_at: str,
    message_id: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO deliveries(post_url, delivered_at, provider, message_id)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(post_url, provider) DO UPDATE SET
            delivered_at=excluded.delivered_at,
            provider=excluded.provider,
            message_id=excluded.message_id
        """,
        (post_url, delivered_at, provider, message_id),
    )
    conn.commit()


def iter_undelivered_posts(conn: sqlite3.Connection, provider: str, limit: int) -> Iterable[sqlite3.Row]:
    cur = conn.execute(
        """
        SELECT p.*
        FROM posts p
        LEFT JOIN deliveries d
          ON d.post_url = p.url AND d.provider = ?
        WHERE d.post_url IS NULL
        ORDER BY p.scraped_at DESC
        LIMIT ?
        """,
        (provider, limit),
    )
    for row in cur.fetchall():
        yield row


def _migrate_threads(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA table_info(threads)")
    existing = {str(r[1]) for r in cur.fetchall()}

    if "extracted_at" not in existing:
        conn.execute("ALTER TABLE threads ADD COLUMN extracted_at TEXT")
    if "downloads_count" not in existing:
        conn.execute("ALTER TABLE threads ADD COLUMN downloads_count INTEGER NOT NULL DEFAULT 0")
    if "title" not in existing:
        conn.execute("ALTER TABLE threads ADD COLUMN title TEXT")
    if "first_post_text" not in existing:
        conn.execute("ALTER TABLE threads ADD COLUMN first_post_text TEXT")
    if "created_at" not in existing:
        conn.execute("ALTER TABLE threads ADD COLUMN created_at TEXT")
    if "failure_count" not in existing:
        conn.execute("ALTER TABLE threads ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0")

    conn.commit()


def update_thread_meta(
    conn: sqlite3.Connection,
    url: str,
    fetched_at: str,
    title: str | None,
    first_post_text: str | None,
    created_at: str | None,
) -> None:
    conn.execute(
        """
        UPDATE threads
        SET last_fetched_at=?,
            title=COALESCE(?, title),
            first_post_text=COALESCE(?, first_post_text),
            created_at=COALESCE(?, created_at)
        WHERE url=?
        """,
        (fetched_at, title, first_post_text, created_at, url),
    )
    conn.commit()


def upsert_discovered(
    conn: sqlite3.Connection,
    url: str,
    discovered_at: str,
    started_at: str | None = None,
) -> bool:
    before = conn.total_changes
    conn.execute(
        """
        INSERT INTO threads(url, discovered_at, status, last_error, created_at)
        VALUES(?, ?, 'new', NULL, ?)
        ON CONFLICT(url) DO NOTHING
        """,
        (url, discovered_at, started_at),
    )
    conn.commit()
    return conn.total_changes > before


def mark_processing(conn: sqlite3.Connection, url: str) -> None:
    conn.execute("UPDATE threads SET status='processing', last_error=NULL WHERE url=?", (url,))
    conn.commit()


def mark_done(conn: sqlite3.Connection, url: str, fetched_at: str, content_path: str) -> None:
    conn.execute(
        "UPDATE threads SET status='done', last_error=NULL, last_fetched_at=?, content_path=? WHERE url=?",
        (fetched_at, content_path, url),
    )
    conn.commit()


def mark_extracted(conn: sqlite3.Connection, thread_url: str, extracted_at: str, downloads_count: int) -> None:
    conn.execute(
        """
        UPDATE threads
        SET status='done', last_error=NULL, extracted_at=?, downloads_count=?, failure_count=0
        WHERE url=?
        """,
        (extracted_at, downloads_count, thread_url),
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, url: str, error: str) -> None:
    conn.execute(
        """
        UPDATE threads
        SET status='failed',
            last_error=?,
            failure_count=COALESCE(failure_count, 0) + 1
        WHERE url=?
        """,
        (error, url),
    )
    conn.commit()


def iter_pending(conn: sqlite3.Connection, limit: int) -> Iterable[str]:
    cur = conn.execute(
        """
        SELECT url
        FROM threads
        WHERE status IN ('new','failed')
          AND (extracted_at IS NULL)
          AND NOT (status='failed' AND last_error='browser_check')
          AND NOT (status='failed' AND COALESCE(failure_count, 0) >= 2)
        ORDER BY CASE status WHEN 'new' THEN 0 ELSE 1 END, discovered_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    for row in cur.fetchall():
        yield str(row["url"])


def get_thread_created_at(conn: sqlite3.Connection, url: str) -> str | None:
    row = conn.execute("SELECT created_at FROM threads WHERE url=? LIMIT 1", (url,)).fetchone()
    if not row:
        return None
    v = row[0]
    return (str(v) if v is not None else None)


def get_record(conn: sqlite3.Connection, url: str) -> Optional[ThreadRecord]:
    cur = conn.execute("SELECT url, discovered_at, status, last_error FROM threads WHERE url=?", (url,))
    row = cur.fetchone()
    if not row:
        return None
    return ThreadRecord(
        url=str(row["url"]),
        discovered_at=str(row["discovered_at"]),
        status=str(row["status"]),
        last_error=(str(row["last_error"]) if row["last_error"] is not None else None),
    )
