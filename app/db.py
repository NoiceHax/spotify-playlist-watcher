"""SQLite persistence for chimichungus-watcher.

Single-file DB at data/watcher.db (mounted as a volume in Docker).

Tables:
  meta(key, value)                       - kv cache (e.g. playlist_id)
  notified(track_id, direction, ...)     - diffs already alerted on (dedup)
  runs(...)                              - per-poll summary history
  track_genre(track_id, genre, ...)      - cached iTunes genre per track
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notified (
    track_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    name TEXT DEFAULT '',
    artists TEXT DEFAULT '',
    first_seen TEXT NOT NULL,
    last_notified TEXT NOT NULL,
    PRIMARY KEY (track_id, direction)
);
CREATE TABLE IF NOT EXISTS excluded_track (
    track_id   TEXT PRIMARY KEY,
    reason     TEXT NOT NULL DEFAULT '',
    excluded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artist_genre (
    artist     TEXT PRIMARY KEY,
    verdict    INTEGER,          -- 1 rock, 0 not, NULL undecided
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS track_genre (
    track_id TEXT PRIMARY KEY,
    genre TEXT,
    source TEXT DEFAULT '',
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    playlist_id TEXT DEFAULT '',
    liked_count INTEGER NOT NULL,
    playlist_count INTEGER NOT NULL,
    liked_missing INTEGER NOT NULL,
    orphaned INTEGER NOT NULL,
    new_alerts INTEGER NOT NULL
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA)
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def is_notified(conn: sqlite3.Connection, track_id: str, direction: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM notified WHERE track_id = ? AND direction = ?",
            (track_id, direction),
        ).fetchone()
        is not None
    )


def mark_notified(
    conn: sqlite3.Connection,
    track_id: str,
    direction: str,
    name: str = "",
    artists: str = "",
) -> None:
    ts = now_iso()
    conn.execute(
        "INSERT INTO notified(track_id, direction, name, artists, first_seen, last_notified)"
        " VALUES(?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(track_id, direction) DO UPDATE SET"
        " name = excluded.name, artists = excluded.artists, last_notified = excluded.last_notified",
        (track_id, direction, name, artists, ts, ts),
    )


def prune_resolved(
    conn: sqlite3.Connection, direction: str, current_ids: set[str]
) -> int:
    """Drop notified rows that no longer differ (e.g. track added to playlist).

    Returns number of rows pruned.
    """
    rows = conn.execute(
        "SELECT track_id FROM notified WHERE direction = ?", (direction,)
    ).fetchall()
    stale = [r["track_id"] for r in rows if r["track_id"] not in current_ids]
    for tid in stale:
        conn.execute(
            "DELETE FROM notified WHERE track_id = ? AND direction = ?",
            (tid, direction),
        )
    return len(stale)


def record_run(
    conn: sqlite3.Connection,
    *,
    playlist_id: str,
    liked_count: int,
    playlist_count: int,
    liked_missing: int,
    orphaned: int,
    new_alerts: int,
) -> None:
    conn.execute(
        "INSERT INTO runs(started_at, playlist_id, liked_count, playlist_count,"
        " liked_missing, orphaned, new_alerts) VALUES(?, ?, ?, ?, ?, ?, ?)",
        (now_iso(), playlist_id, liked_count, playlist_count,
         liked_missing, orphaned, new_alerts),
    )
    conn.commit()
    # Keep history bounded; SQLite has no TRUNCATE so delete old rows.
    conn.execute(
        "DELETE FROM runs WHERE id NOT IN "
        "(SELECT id FROM runs ORDER BY id DESC LIMIT 500)"
    )
    conn.commit()
