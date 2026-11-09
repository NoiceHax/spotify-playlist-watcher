"""Compare Liked Songs vs the Chimichungus playlist.

Directions:
  liked_missing: in Liked Songs but NOT in the playlist (candidate to add)
  orphaned:      in the playlist but NOT in Liked Songs (removed from Liked?)

Only *new* diffs (not already in the notified table) are returned as alerts,
so Discord doesn't get spammed every poll. Resolved diffs are pruned so a
re-appearing track alerts again.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from . import db, genre as genre_mod
from .spotify import SpotifyClient, Track

log = logging.getLogger("chimichungus.sync")

LIKED_MISSING = "liked_not_in_playlist"
ORPHANED = "playlist_not_in_liked"


@dataclass
class SyncResult:
    playlist_id: str
    playlist_name: str
    liked_count: int
    playlist_count: int
    liked_missing: list[Track]
    orphaned: list[Track]
    new_alerts: list[tuple[str, Track]]  # (direction, track)
    pruned: int = 0
    added: list[Track] | None = None


def run_once(conn: sqlite3.Connection, sp: SpotifyClient, playlist_name: str,
             auto_add_missing: bool = False,
             genre_filter: str = "rock") -> SyncResult:
    playlist_id = db.get_meta(conn, "playlist_id")
    resolved_name = playlist_name
    if playlist_id:
        try:
            tracks_probe = sp.get_playlist_tracks(playlist_id)
            _probe_ok = True
        except Exception as exc:  # playlist deleted/renamed -> re-search by name
            log.warning("Cached playlist_id %s failed (%s); re-searching by name",
                        playlist_id, exc)
            _probe_ok = False
            tracks_probe = {}
        if not _probe_ok:
            playlist_id = None
    if not playlist_id:
        playlist_id, resolved_name = sp.find_playlist_by_name(playlist_name)
        db.set_meta(conn, "playlist_id", playlist_id)
        db.set_meta(conn, "playlist_name", resolved_name)
        tracks_probe = {}

    liked = sp.get_saved_tracks()
    playlist_tracks = tracks_probe if tracks_probe else sp.get_playlist_tracks(playlist_id)

    liked_ids = set(liked)
    playlist_ids = set(playlist_tracks)

    missing_ids = liked_ids - playlist_ids
    orphaned_ids = playlist_ids - liked_ids

    missing = [liked[i] for i in sorted(missing_ids)]
    orphaned = [playlist_tracks[i] for i in sorted(orphaned_ids)]

    new_alerts: list[tuple[str, Track]] = []
    for t in missing:
        if not db.is_notified(conn, t.id, LIKED_MISSING):
            new_alerts.append((LIKED_MISSING, t))
    for t in orphaned:
        if not db.is_notified(conn, t.id, ORPHANED):
            new_alerts.append((ORPHANED, t))

    for direction, t in new_alerts:
        db.mark_notified(conn, t.id, direction, t.name, t.artists)

    pruned = db.prune_resolved(conn, LIKED_MISSING, missing_ids)
    pruned += db.prune_resolved(conn, ORPHANED, orphaned_ids)

    # Tracks deliberately removed (e.g. duplicate copies) must never be
    # re-added, or dedupe and auto-add fight each other forever.
    excluded = {r["track_id"] for r in
                conn.execute("SELECT track_id FROM excluded_track")}
    if excluded:
        before_n = len(missing)
        missing = [t for t in missing if t.id not in excluded]
        if before_n != len(missing):
            log.info("ignoring %d excluded track(s)", before_n - len(missing))

    added_tracks: list[Track] = []
    if auto_add_missing and missing:
        if genre_filter == "rock":
            rock, other, unknown = genre_mod.filter_rock(conn, missing)
            log.info("genre filter: rock=%d other=%d unknown=%d (of %d missing)",
                     len(rock), len(other), len(unknown), len(missing))
            to_add = [t for t, _g in rock]
            if unknown:
                log.info("skipping %d track(s) with no genre data (never auto-added)",
                         len(unknown))
        else:
            to_add = list(missing)
        added_tracks = to_add
        if to_add:
            log.info("adding %d track(s) to playlist %s", len(to_add), playlist_id)
            sp.add_to_playlist(playlist_id, [t.uri for t in to_add])
        else:
            log.info("nothing to add after genre filter")
        # They will resolve on next poll and be pruned then.

    conn.commit()
    db.record_run(
        conn,
        playlist_id=playlist_id,
        liked_count=len(liked),
        playlist_count=len(playlist_tracks),
        liked_missing=len(missing),
        orphaned=len(orphaned),
        new_alerts=len(new_alerts),
    )

    log.info("liked=%d playlist=%d missing=%d orphaned=%d new=%d pruned=%d",
             len(liked), len(playlist_tracks), len(missing),
             len(orphaned), len(new_alerts), pruned)
    return SyncResult(
        playlist_id=playlist_id,
        playlist_name=resolved_name,
        liked_count=len(liked),
        playlist_count=len(playlist_tracks),
        liked_missing=missing,
        orphaned=orphaned,
        new_alerts=new_alerts,
        pruned=pruned,
        added=added_tracks,
    )
