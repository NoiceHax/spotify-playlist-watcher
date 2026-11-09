"""spotify-playlist-watcher: keep a playlist in step with Liked Songs, alert to Discord.

Single container = OAuth token refresh + SQLite + async poll loop.

Env:
  SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REFRESH_TOKEN (required)
  PLAYLIST_NAME          (default: Chimichungus)
  DISCORD_WEBHOOK_URL    (optional; without it we only log)
  DISCORD_ALWAYS_POST    (optional; post even on a quiet run - heartbeat)
  POLL_INTERVAL_SECONDS  (default: 1800)
  DB_PATH                (default: /app/data/watcher.db, falls back to ./data/watcher.db)
  SYNC_ADD_MISSING       (default: false — auto-add Liked Songs missing from playlist)
  GENRE_FILTER           (default: rock — only auto-add rock/metal; 'none' = add all)
  LOG_LEVEL              (default: INFO)

Run once:  python -m app.main --once
Loop:      python -m app.main
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

import requests

from . import db
from .spotify import SpotifyClient
from .sync import LIKED_MISSING, run_once

log = logging.getLogger("chimichungus")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


def default_db_path() -> str:
    for candidate in ("/app/data/watcher.db", "data/watcher.db"):
        parent = candidate.rsplit("/", 1)[0]
        try:
            if os.path.isdir("/app") and candidate.startswith("/app"):
                return candidate
        except OSError:
            pass
    # Local dev fallback: relative to repo root (parent of app/)
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "data", "watcher.db")


def _chunk_lines(lines: list, cap: int = 1900) -> list:
    """Split lines into Discord-sized messages WITHOUT dropping any."""
    chunks, cur, n = [], [], 0
    for ln in lines:
        if len(ln) > cap:               # pathological single line
            ln = ln[:cap - 3] + "..."
        if n + len(ln) + 1 > cap and cur:
            chunks.append(chr(10).join(cur))
            cur, n = [], 0
        cur.append(ln)
        n += len(ln) + 1
    if cur:
        chunks.append(chr(10).join(cur))
    return chunks


def post_discord(webhook: str, content) -> None:
    """Post one message, or a list of lines split across as many messages
    as it takes. Never silently drops names."""
    import time
    if isinstance(content, str):
        parts = _chunk_lines(content.split(chr(10)))
    else:
        parts = _chunk_lines(list(content))
    total = len(parts)
    for i, part in enumerate(parts, 1):
        body = part if total == 1 else f"{part}{chr(10)}_(part {i}/{total})_"
        try:
            resp = requests.post(webhook, json={"content": body}, timeout=15)
            if resp.status_code == 429:
                wait = 2.0
                try:
                    wait = float(resp.json().get("retry_after", 2.0))
                except Exception:
                    pass
                time.sleep(min(wait + 0.3, 10))
                resp = requests.post(webhook, json={"content": body}, timeout=15)
            if resp.status_code not in (200, 204):
                log.warning("Discord webhook failed (%s) on part %d/%d: %s",
                            resp.status_code, i, total, resp.text[:200])
        except Exception as exc:
            log.warning("Discord webhook error on part %d/%d: %s", i, total, exc)
        if i < total:
            time.sleep(0.7)  # stay under the webhook rate limit


def format_alerts(result) -> list:
    """Full report as a list of lines. No truncation - every name is listed."""
    added = list(result.added or [])
    added_ids = {t.id for t in added}

    lines = [
        f"**Chimichungus watcher** - liked={result.liked_count} "
        f"playlist `{result.playlist_name}`={result.playlist_count}"
    ]

    if added:
        lines.append("")
        lines.append(f"**Added {len(added)} rock/metal track(s):**")
        for t in added:
            lines.append(f"- {t.name} - {t.artists}")

    # Tracks added this run are no longer missing; do not double-report them.
    fresh_missing = [t for d, t in result.new_alerts
                     if d == LIKED_MISSING and t.id not in added_ids]
    fresh_orphaned = [t for d, t in result.new_alerts if d != LIKED_MISSING]

    if fresh_missing:
        lines.append("")
        lines.append(f"**{len(fresh_missing)} new Liked Song(s) not added "
                     f"(filtered out or unknown genre):**")
        for t in fresh_missing:
            lines.append(f"- {t.name} - {t.artists}")

    if fresh_orphaned:
        lines.append("")
        lines.append(f"**{len(fresh_orphaned)} playlist track(s) no longer Liked:**")
        for t in fresh_orphaned:
            lines.append(f"- {t.name} - {t.artists}")

    if not added and not fresh_missing and not fresh_orphaned:
        lines.append(
            f"Nothing new this week (still missing={len(result.liked_missing)}, "
            f"orphaned={len(result.orphaned)}). Watcher is alive.")

    return lines


def do_poll() -> bool:
    playlist_name = env("PLAYLIST_NAME", "Chimichungus")
    webhook = env("DISCORD_WEBHOOK_URL")
    db_path = env("DB_PATH", default_db_path())
    auto_add = truthy(env("SYNC_ADD_MISSING", "false"))

    sp = SpotifyClient(
        env("SPOTIFY_CLIENT_ID"),
        env("SPOTIFY_CLIENT_SECRET"),
        env("SPOTIFY_REFRESH_TOKEN"),
    )
    conn = db.connect(db_path)
    try:
        result = run_once(conn, sp, playlist_name, auto_add_missing=auto_add,
                          genre_filter=env("GENRE_FILTER", "rock"))
    finally:
        pass  # keep conn open for record; close below
    try:
        always = truthy(env("DISCORD_ALWAYS_POST", "false"))
        has_news = bool(result.new_alerts or result.added)
        if webhook and (has_news or always):
            post_discord(webhook, format_alerts(result))
        elif result.new_alerts:
            log.info("No DISCORD_WEBHOOK_URL; new diffs only logged")
            for direction, t in result.new_alerts:
                log.info("  [%s] %s — %s", direction, t.name, t.artists)
        if result.added:
            log.info("ADDED %d track(s) to '%s':", len(result.added), result.playlist_name)
            for t in result.added:
                log.info("  + %s — %s", t.name, t.artists)
        return True
    finally:
        conn.close()


async def loop_forever(interval: int) -> None:
    stop = asyncio.Event()

    def _handle(*_args):
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, _handle)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda *_a: stop.set())

    log.info("Watching playlist '%s' every %ss", env("PLAYLIST_NAME", "Chimichungus"), interval)
    while not stop.is_set():
        try:
            await asyncio.to_thread(do_poll)
        except Exception:
            log.exception("Poll failed; will retry in %ss", interval)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log.info("Shutting down")


def main() -> int:
    parser = argparse.ArgumentParser(description="Chimichungus Spotify watcher")
    parser.add_argument("--once", action="store_true", help="run one poll then exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, env("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    missing = [k for k in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET",
                           "SPOTIFY_REFRESH_TOKEN") if not env(k)]
    if missing:
        log.error("Missing required env: %s (see .env.example)", ", ".join(missing))
        return 2
    try:
        interval = int(env("POLL_INTERVAL_SECONDS", "1800"))
    except ValueError:
        log.error("POLL_INTERVAL_SECONDS must be an integer")
        return 2

    if args.once:
        try:
            do_poll()
            return 0
        except Exception:
            log.exception("One-shot poll failed")
            return 1
    asyncio.run(loop_forever(max(interval, 60)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
