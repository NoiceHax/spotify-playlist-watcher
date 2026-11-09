"""Per-track genre lookup via the iTunes Search API, cached in SQLite.

Spotify removed genre data from its API (artist `genres` is gone, batch
/artists and /audio-features return 403), so genre comes from iTunes:
public, no key, and per-track rather than per-artist.

Lookups are cached in track_genre forever — a track's genre does not change,
so a weekly run only pays for genuinely new Liked Songs.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

log = logging.getLogger("chimichungus.genre")

SEARCH = "https://itunes.apple.com/search"
UA = "chimichungus-watcher/1.0"

# iTunes primaryGenreName values that count as rock/metal or a subgenre.
ROCK = {
    "rock", "metal", "hard rock", "heavy metal", "alternative rock",
    "punk", "punk rock", "pop punk", "post-punk", "hardcore", "hardcore punk",
    "grunge", "emo", "screamo", "metalcore", "deathcore", "death metal",
    "black metal", "thrash metal", "doom metal", "sludge metal", "power metal",
    "progressive metal", "progressive rock", "prog-rock/art rock", "psychedelic",
    "psychedelic rock", "indie rock", "industrial", "goth rock", "gothic metal",
    "nu metal", "rock & roll", "rock and roll", "classic rock", "southern rock",
    "arena rock", "glam rock", "garage rock", "surf rock", "shoegaze",
    "post-rock", "math rock", "stoner rock", "britpop", "new wave",
    "american trad rock", "adult alternative", "college rock", "hair metal",
    "rap metal", "funk metal", "speed metal", "grindcore", "post-hardcore",
    "alternative metal", "symphonic metal", "folk-rock", "blues-rock",
    "jam bands", "hardcore metal",
}
# iTunes returns a bare "Alternative" for everything from Fall Out Boy to
# Dance Monkey, so it cannot decide rock on its own. These genres get a
# second opinion from MusicBrainz artist tags before we trust them.
AMBIGUOUS = {"alternative", "alt", "indie"}

NOT_ROCK = {
    "pop", "dance", "hip-hop/rap", "hip hop/rap", "r&b/soul", "electronic",
    "soundtrack", "singer/songwriter", "country", "indie pop", "k-pop",
    "world", "reggae", "jazz", "blues", "classical", "folk", "latin",
    "bollywood", "indian pop", "regional indian", "indian", "worldwide",
    "vocal", "easy listening", "new age", "holiday", "comedy", "spoken word",
    "anime", "j-pop", "children's music", "instrumental",
}


def is_rock(genre: str | None) -> bool | None:
    """True = rock/metal, False = other, None = unknown (never auto-added)."""
    if not genre:
        return None
    s = genre.strip().lower()
    if s in ROCK:
        return True
    if s in NOT_ROCK:
        return False
    if "metal" in s or "punk" in s:
        return True
    if s.startswith("rock") or s.endswith(" rock"):
        return True
    return False


def _strip_light(s: str) -> str:
    s = re.sub(r"\s*[-–—]\s*(Remastered|Remaster|\d{4} Remaster).*$", "", s, flags=re.I)
    s = re.sub(r"\s*\((feat\.|with |From |Acoustic|Live|Remix|Extended).*?\)", "", s, flags=re.I)
    return re.sub(r"\s*\[.*?\]", "", s).strip()


def _strip_hard(s: str) -> str:
    s = re.sub(r"\s*[-–—]\s*.*$", "", s)
    s = re.sub(r"\s*\(.*?\)", "", s)
    return re.sub(r"\s*\[.*?\]", "", s).strip()


def _search(term: str, limit: int = 5, tries: int = 3) -> dict | None:
    url = f"{SEARCH}?" + urllib.parse.urlencode(
        {"term": term, "entity": "song", "limit": limit})
    for attempt in range(tries):
        try:
            rq = urllib.request.Request(url, headers={"User-Agent": UA})
            return json.load(urllib.request.urlopen(rq, timeout=25))
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429, 503):
                time.sleep(3 * (attempt + 1))
                continue
            return None
        except Exception:
            time.sleep(2)
    return None


def _pick(results: list, artist: str) -> dict | None:
    for r in results:
        if artist and artist.lower() in (r.get("artistName") or "").lower():
            return r
    return None


def lookup(track_name: str, artist: str, pause: float = 0.35) -> tuple[str | None, str]:
    """Return (genre, how). Tries exact title, loose title, then artist-level."""
    for cleaner, tag in ((_strip_light, "track"), (_strip_hard, "track_loose")):
        j = _search(f"{cleaner(track_name)} {artist}".strip())
        time.sleep(pause)
        if j:
            hit = _pick(j.get("results") or [], artist)
            if hit and hit.get("primaryGenreName"):
                return hit["primaryGenreName"], tag

    j = _search(artist, limit=12)
    time.sleep(pause)
    if j:
        gs = [r.get("primaryGenreName") for r in (j.get("results") or [])
              if (r.get("artistName") or "").lower() == artist.lower()]
        gs = [g for g in gs if g]
        if gs:
            return Counter(gs).most_common(1)[0][0], "artist"
    return None, "unknown"


def genre_for(conn: sqlite3.Connection, track_id: str,
              track_name: str, artist: str) -> tuple[str | None, str]:
    row = conn.execute(
        "SELECT genre, source FROM track_genre WHERE track_id = ?", (track_id,)
    ).fetchone()
    if row is not None:
        return row["genre"], row["source"]

    genre, how = lookup(track_name, artist)
    from . import db
    conn.execute(
        "INSERT OR REPLACE INTO track_genre (track_id, genre, source, checked_at) "
        "VALUES (?, ?, ?, ?)", (track_id, genre, how, db.now_iso()))
    conn.commit()
    return genre, how


# --- MusicBrainz artist-tag tiebreaker -------------------------------------
# Only ever called for AMBIGUOUS iTunes genres, so the slow API is hit rarely.
MB_URL = "https://musicbrainz.org/ws/2/artist"
MB_UA = "chimichungus-watcher/1.0 (homelab personal use)"

# Tags that settle the question one way or the other.
MB_ROCK_TAGS = ("rock", "metal", "punk", "grunge", "emo", "hardcore",
                "shoegaze", "britpop", "post-rock", "metalcore")
MB_NOT_TAGS = ("pop", "r&b", "hip hop", "rap", "soul", "electronic", "house",
               "edm", "country", "folk pop", "singer-songwriter", "bollywood",
               "k-pop", "jazz", "classical", "trap", "lo-fi")


def _mb_artist_tags(artist: str, pause: float = 1.1) -> list:
    """Fetch MusicBrainz tags for an artist. Returns [] on any failure."""
    import time
    params = urllib.parse.urlencode(
        {"query": f'artist:"{artist}"', "fmt": "json", "limit": 1})
    req = urllib.request.Request(MB_URL + "?" + params,
                                 headers={"User-Agent": MB_UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.load(r)
    except Exception as exc:
        log.debug("musicbrainz lookup failed for %s: %s", artist, exc)
        return []
    finally:
        time.sleep(pause)  # MB asks for <= 1 req/sec
    arts = data.get("artists") or []
    if not arts:
        return []
    top = arts[0]
    if (top.get("name") or "").strip().lower() != artist.strip().lower():
        return []  # fuzzy match on a different artist - do not trust it
    return [t.get("name", "").lower() for t in (top.get("tags") or [])]


def _tag_verdict(tag: str) -> bool | None:
    """Classify ONE tag. Rock wins compounds: 'pop rock' and 'pop punk' are rock."""
    t = tag.strip().lower()
    if not t:
        return None
    # Rock-side checks come first so compound tags are not double-counted.
    if t in ("rock", "metal", "punk", "emo", "grunge", "hardcore", "shoegaze",
             "britpop", "post-britpop", "indie rock", "pop/rock", "rock and indie"):
        return True
    if t.endswith(" rock") or t.startswith("rock ") or "/rock" in t:
        return True
    for key in ("metal", "punk", "grunge", "shoegaze", "metalcore",
                "hardcore", "post-rock", "emo"):
        if key in t:
            return True
    for key in ("pop", "r&b", "hip hop", "hip-hop", "rap", "soul", "electronic",
                "house", "edm", "country", "singer-songwriter", "bollywood",
                "k-pop", "jazz", "classical", "trap", "lo-fi", "trip-hop",
                "indietronica", "disco", "funk", "reggae", "folk"):
        if key in t:
            return False
    return None


def _tags_say_rock(tags: list) -> bool | None:
    """True/False from artist tags, or None if they do not settle it."""
    if not tags:
        return None
    rock_hits = not_hits = 0
    for t in tags:
        v = _tag_verdict(t)
        if v is True:
            rock_hits += 1
        elif v is False:
            not_hits += 1
    if not rock_hits and not not_hits:
        return None
    if rock_hits and rock_hits >= not_hits:
        return True
    return False


def artist_is_rock(conn, artist: str) -> bool | None:
    """Cached per-artist rock verdict from MusicBrainz tags."""
    row = conn.execute(
        "SELECT verdict FROM artist_genre WHERE artist = ?", (artist,)
    ).fetchone()
    if row is not None:
        v = row["verdict"]
        return None if v is None else bool(v)

    tags = _mb_artist_tags(artist)
    if not tags:
        # Transient failure or genuinely untagged: do NOT cache, so a later
        # run can try again instead of being stuck on "undecided" forever.
        return None
    verdict = _tags_say_rock(tags)
    from . import db
    conn.execute(
        "INSERT OR REPLACE INTO artist_genre (artist, verdict, checked_at) "
        "VALUES (?, ?, ?)",
        (artist, None if verdict is None else int(verdict), db.now_iso()))
    conn.commit()
    return verdict


def filter_rock(conn: sqlite3.Connection, tracks: list) -> tuple[list, list, list]:
    """Split tracks into (rock, other, unknown) by cached/looked-up genre."""
    rock, other, unknown = [], [], []
    for t in tracks:
        primary = (t.artists or "").split(",")[0].strip()
        genre, _how = genre_for(conn, t.id, t.name, primary)
        verdict = is_rock(genre)
        if (genre or "").strip().lower() in AMBIGUOUS:
            # iTunes cannot settle this bucket; ask MusicBrainz about the artist.
            verdict = artist_is_rock(conn, primary)
            log.debug("ambiguous '%s' for %s - artist verdict: %s",
                      genre, primary, verdict)
        elif verdict is True:
            # iTunes is sometimes confidently wrong (it files SHINee under
            # "Rock"). A positive not-rock verdict from MusicBrainz vetoes it;
            # an undecided one (None) leaves iTunes alone.
            second = artist_is_rock(conn, primary)
            if second is False:
                log.info("veto: %s - iTunes said '%s' but artist tags say not rock",
                         primary, genre)
                verdict = False
        if verdict is True:
            rock.append((t, genre))
        elif verdict is False:
            other.append((t, genre))
        else:
            unknown.append((t, genre))
    return rock, other, unknown
