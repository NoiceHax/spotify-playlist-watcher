"""One-off: build a playlist of Liked Songs that are rock/metal but sound soft.

Spotify's /audio-features is 403 for this app, so features come from
ReccoBeats (free, keyed by Spotify track id). Genre reuses app.genre, run
against a COPY of watcher.db so the live watcher state is never touched.

  python3 -m scripts.soft_playlist --db /path/to/watcher-copy.db --out report.csv
  python3 -m scripts.soft_playlist ... --overrides review.json --apply --name "..."

review.json maps track_id -> {"verdict": "soft"|"heavy", "reason": "..."};
it settles borderline/no-data tracks and can flip any automatic verdict.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

import requests

from app import genre
from app.spotify import API_BASE, SpotifyClient

log = logging.getLogger("soft")

RECCO = "https://api.reccobeats.com/v1/audio-features"
CACHE = Path(__file__).resolve().parent.parent / "data" / "soft_cache.json"
REFERENCE = "4OmlsAT8r4q9vPFBvfYgyZ"  # Loathe - Is It Really You?
FEATURES = ("energy", "acousticness", "loudness", "instrumentalness",
            "valence", "tempo", "danceability")


def fetch_features(ids: list[str]) -> dict[str, dict | None]:
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    todo = [i for i in ids if i not in cache]
    for n in range(0, len(todo), 40):
        chunk = todo[n:n + 40]
        for attempt in range(3):
            resp = requests.get(RECCO, params={"ids": ",".join(chunk)}, timeout=30)
            if resp.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            resp.raise_for_status()
            break
        got = {}
        for f in resp.json().get("content", []):
            sid = (f.get("href") or "").rsplit("/", 1)[-1]
            got[sid] = {k: f.get(k) for k in FEATURES}
        for sid in chunk:
            cache[sid] = got.get(sid)  # None = ReccoBeats has no data
        CACHE.write_text(json.dumps(cache))
        log.info("features %d/%d", min(n + 40, len(todo)), len(todo))
        time.sleep(0.5)
    return {i: cache.get(i) for i in ids}


def softness(f: dict) -> float:
    # loudness: -4 dB (brickwalled) -> 0, -16 dB (quiet) -> 1
    loud = min(max((-f["loudness"] - 4) / 12, 0.0), 1.0)
    return round((1 - f["energy"]) * 0.5 + f["acousticness"] * 0.2 + loud * 0.3, 4)


def create_playlist(sp: SpotifyClient, name: str) -> str:
    resp = requests.post(
        f"{API_BASE}/me/playlists",
        headers={"Authorization": f"Bearer {sp._token()}"},
        json={"name": name, "public": False,
              "description": "Liked rock/metal that sounds soft."},
        timeout=20)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"create playlist failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()["id"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="copy of watcher.db (genre cache)")
    ap.add_argument("--out", required=True, help="report CSV path")
    ap.add_argument("--overrides", help="review JSON: track_id -> verdict/reason")
    ap.add_argument("--margin", type=float, default=0.06,
                    help="score band below the reference that counts as borderline")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--name", default="Soft Heavy (untitled)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    sp = SpotifyClient(os.environ["SPOTIFY_CLIENT_ID"],
                       os.environ["SPOTIFY_CLIENT_SECRET"],
                       os.environ["SPOTIFY_REFRESH_TOKEN"])
    liked = sp.get_saved_tracks()
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rock, _other, unknown = genre.filter_rock(conn, list(liked.values()))
    pool = [(t, g, "rock") for t, g in rock] + [(t, g, "unknown") for t, g in unknown]

    feats = fetch_features([t.id for t, _, _ in pool])
    ref = feats.get(REFERENCE)
    bar = softness(ref) if ref else 0.25
    overrides = json.loads(Path(args.overrides).read_text()) if args.overrides else {}

    rows = []
    for t, g, bucket in pool:
        f = feats.get(t.id)
        score = softness(f) if f else None
        if bucket == "unknown":
            auto = "borderline"
        elif score is None:
            auto = "no-data"
        elif score >= bar:
            auto = "soft"
        elif score >= bar - args.margin:
            auto = "borderline"
        else:
            auto = "heavy"
        o = overrides.get(t.id, {})
        rows.append({
            "track_id": t.id, "name": t.name, "artists": t.artists,
            "genre": g or "", "bucket": bucket,
            **{k: (f or {}).get(k, "") for k in FEATURES},
            "score": "" if score is None else score, "auto": auto,
            "verdict": o.get("verdict", auto), "reason": o.get("reason", ""),
            "uri": t.uri,
        })
    rows.sort(key=lambda r: (r["verdict"], -(r["score"] or 0)))

    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    count = lambda key, v: sum(1 for r in rows if r[key] == v)
    log.info("liked=%d rock=%d unknown-genre=%d reference score=%.3f",
             len(liked), len(rock), len(unknown), bar)
    for v in ("soft", "borderline", "heavy", "no-data"):
        log.info("  auto %-10s %4d   final %4d", v, count("auto", v), count("verdict", v))

    if args.apply:
        pending = count("verdict", "borderline") + count("verdict", "no-data")
        if pending:
            raise SystemExit(f"{pending} tracks still unreviewed; settle them in --overrides first")
        # The same song is often liked twice under different ids (single vs album).
        seen, uris = set(), []
        for r in rows:
            key = (r["name"].lower(), r["artists"].split(",")[0].strip().lower())
            if r["verdict"] == "soft" and key not in seen:
                seen.add(key)
                uris.append(r["uri"])
        pid = create_playlist(sp, args.name)
        sp.add_to_playlist(pid, uris)
        back = sp.get_playlist_tracks(pid)
        log.info("created playlist %s with %d tracks (read back %d)", pid, len(uris), len(back))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
