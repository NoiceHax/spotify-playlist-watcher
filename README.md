# spotify-playlist-watcher

Keeps a Spotify playlist in step with your Liked Songs. It runs in a single container, polls on an
interval, posts what changed to Discord, and can add new liked songs to the playlist itself,
filtered by genre.

I keep one playlist that's meant to hold all the rock and metal I've liked. Every time I liked a
song I had to remember to add it there too, and I usually didn't. This does it for me.

## What it does

Each poll compares your **Liked Songs** with the **playlist** you name and finds two kinds of
difference:

- **Liked but missing**: liked songs that aren't in the playlist yet.
- **Orphaned**: songs in the playlist that you've since unliked.

New differences are posted to a Discord webhook once, never repeated on later polls, and dropped
from the record once they're resolved. Every run is logged in SQLite.

With `SYNC_ADD_MISSING=true` it also **adds liked-but-missing songs to the playlist**:

- **Genre filter** (`GENRE_FILTER=rock`, the default): only rock and metal get added, `none` adds
  everything. Spotify no longer returns genres through its API, so the genre comes from the iTunes
  Search API per track. When iTunes is vague ("Alternative", "Pop" and similar) the artist's
  MusicBrainz tags decide. MusicBrainz can also veto iTunes when iTunes is confidently wrong. Tracks
  with no usable genre data are never added automatically. Lookups are cached in SQLite forever, so
  a run only pays for songs you liked since the last one.
- **Exclusions**: tracks you removed on purpose (for example duplicate copies) go in an
  `excluded_track` table and are never added back, so cleanup and auto-add don't undo each other.

## Setup

1. Create an app at <https://developer.spotify.com/dashboard> and add the redirect URI
   `http://127.0.0.1:8888/callback`.
2. Get a refresh token:
   ```bash
   cp .env.example .env              # fill SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET
   python3 scripts/gen-spotify-token.py
   ```
   Paste the printed `SPOTIFY_REFRESH_TOKEN` into `.env`. The helper requests read and
   playlist-modify scopes so auto-add works.
3. Set `PLAYLIST_NAME` (exact name, case-insensitive) and, optionally, `DISCORD_WEBHOOK_URL`.

## Run

```bash
docker compose up -d --build
docker compose logs -f
docker compose run --rm spotify-watcher python -m app.main --once    # one poll, then exit
```

Without Docker: `pip install -r requirements.txt && python -m app.main --once`.

| Variable | Default | |
|---|---|---|
| `PLAYLIST_NAME` | `Chimichungus` | Playlist to keep in step |
| `POLL_INTERVAL_SECONDS` | `1800` | Minimum 60 |
| `SYNC_ADD_MISSING` | `false` | Add liked-but-missing tracks to the playlist |
| `GENRE_FILTER` | `rock` | `rock` or `none` |
| `DISCORD_WEBHOOK_URL` | | Without it, changes are only logged |
| `DISCORD_ALWAYS_POST` | `false` | Post on quiet runs too, as a heartbeat |
| `DB_PATH` | `/app/data/watcher.db` | SQLite state |

## Extra: soft-rock playlist

`scripts/soft_playlist.py` is a one-off that builds a playlist of liked rock and metal songs that
*sound* soft. Spotify's audio-features endpoint is closed to new apps, so it gets audio features
from ReccoBeats and scores softness from energy, acousticness and loudness. It runs against a copy
of the database so the live watcher is never touched, writes a CSV for review, and takes manual
overrides before `--apply` creates the playlist.
