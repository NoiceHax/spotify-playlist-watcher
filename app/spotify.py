"""Minimal Spotify Web API client using OAuth refresh-token grant.

Env required:
  SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REFRESH_TOKEN

Scopes the refresh token must have been granted:
  user-library-read, playlist-read-private, playlist-read-collaborative
  (+ playlist-modify-public / playlist-modify-private only if SYNC_ADD_MISSING=true)

No third-party Spotify SDK — plain ``requests`` so the image stays tiny.
"""
from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass

import requests

log = logging.getLogger("chimichungus.spotify")

TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"


@dataclass
class Track:
    id: str
    name: str
    artists: str
    album: str = ""
    uri: str = ""
    added_at: str = ""


def _track_from_item(item: dict, added_at: str = "") -> Track | None:
    if isinstance(item.get("track"), dict):
        track = item["track"]
    elif isinstance(item.get("item"), dict):
        track = item["item"]  # playlists/{id}/items renamed the wrapper key
    else:
        track = item
    if not track or not track.get("id"):
        return None  # removed/unavailable track -> null entry in playlist API
    artists = ", ".join(a.get("name", "") for a in track.get("artists", []))
    return Track(
        id=track["id"],
        name=track.get("name", ""),
        artists=artists,
        album=(track.get("album") or {}).get("name", ""),
        uri=track.get("uri", f"spotify:track:{track['id']}"),
        added_at=added_at,
    )


class SpotifyClient:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str,
                 timeout: int = 20):
        if not (client_id and client_secret and refresh_token):
            raise ValueError(
                "SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET / "
                "SPOTIFY_REFRESH_TOKEN must all be set"
            )
        self._cid = client_id
        self._secret = client_secret
        self._refresh = refresh_token
        self._timeout = timeout
        self._access: str | None = None
        self._expires_at = 0.0

    # -- auth ----------------------------------------------------------
    def _refresh_access_token(self) -> str:
        basic = base64.b64encode(f"{self._cid}:{self._secret}".encode()).decode()
        resp = requests.post(
            TOKEN_URL,
            headers={"Authorization": f"Basic {basic}"},
            data={"grant_type": "refresh_token", "refresh_token": self._refresh},
            timeout=self._timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Spotify token refresh failed ({resp.status_code}): {resp.text[:300]}")
        body = resp.json()
        self._access = body["access_token"]
        self._expires_at = time.time() + int(body.get("expires_in", 3600)) - 60
        # Spotify may rotate the refresh token; log a hint (don't print it).
        if body.get("refresh_token"):
            log.warning("Spotify returned a new refresh token — update SPOTIFY_REFRESH_TOKEN")
        return self._access

    def _token(self) -> str:
        if not self._access or time.time() >= self._expires_at:
            return self._refresh_access_token()
        return self._access

    # -- low-level GET with 401/429 handling ---------------------------
    def _get(self, path: str, params: dict | None = None) -> dict:
        url = path if path.startswith("http") else API_BASE + path
        for attempt in range(2):
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {self._token()}"},
                params=params,
                timeout=self._timeout,
            )
            if resp.status_code == 401 and attempt == 0:
                self._access = None  # force refresh and retry once
                continue
            if resp.status_code == 429:
                retry = int(resp.headers.get("Retry-After", "5"))
                log.warning("Spotify rate-limited; sleeping %ss", retry)
                time.sleep(retry)
                continue
            if resp.status_code != 200:
                raise RuntimeError(f"Spotify GET {path} failed ({resp.status_code}): {resp.text[:300]}")
            return resp.json()
        raise RuntimeError(f"Spotify GET {path} failed after retry")

    def _paginate(self, path: str, params: dict | None = None) -> list[dict]:
        items: list[dict] = []
        url: str | None = path
        first = True
        while url:
            page = self._get(url, params if first else None)
            first = False
            items.extend(page.get("items", []))
            url = page.get("next")
        return items

    # -- high-level ----------------------------------------------------
    def get_saved_tracks(self) -> dict[str, Track]:
        """All Liked Songs (user-library-read). Returns id -> Track."""
        out: dict[str, Track] = {}
        for item in self._paginate("/me/tracks", {"limit": 50}):
            t = _track_from_item(item, item.get("added_at", ""))
            if t:
                out[t.id] = t
        return out

    def find_playlist_by_name(self, name: str) -> tuple[str, str]:
        """Find playlist id by name (case-insensitive). Returns (id, name).

        Prefers playlists owned by the current user on name collisions.
        """
        me = self._get("/me")
        my_id = me.get("id", "")
        want = name.strip().lower()
        candidates: list[dict] = []
        for pl in self._paginate("/me/playlists", {"limit": 50}):
            if (pl.get("name") or "").strip().lower() == want:
                candidates.append(pl)
        if not candidates:
            raise RuntimeError(
                f"Playlist '{name}' not found in your library. "
                "Check PLAYLIST_NAME (exact name, case-insensitive)."
            )
        candidates.sort(key=lambda p: 0 if (p.get("owner") or {}).get("id") == my_id else 1)
        chosen = candidates[0]
        if len(candidates) > 1:
            log.warning("Multiple playlists named '%s'; using id=%s owner=%s",
                        name, chosen["id"], (chosen.get("owner") or {}).get("id"))
        return chosen["id"], chosen.get("name", name)

    def get_playlist_tracks(self, playlist_id: str) -> dict[str, Track]:
        out: dict[str, Track] = {}
        fields = "items(added_at,item(id,name,artists,album(name),uri)),next"
        for item in self._paginate(f"/playlists/{playlist_id}/items",
                                   {"limit": 100, "fields": fields}):
            t = _track_from_item(item, item.get("added_at", ""))
            if t:
                out[t.id] = t
        return out

    def add_to_playlist(self, playlist_id: str, uris: list[str]) -> None:
        """Append URIs in chunks of 100. Only used when SYNC_ADD_MISSING=true."""
        for i in range(0, len(uris), 100):
            chunk = uris[i:i + 100]
            resp = requests.post(
                f"{API_BASE}/playlists/{playlist_id}/items",
                headers={"Authorization": f"Bearer {self._token()}",
                         "Content-Type": "application/json"},
                json={"uris": chunk},
                timeout=self._timeout,
            )
            if resp.status_code not in (200, 201):
                raise RuntimeError(
                    f"Spotify add-to-playlist failed ({resp.status_code}): {resp.text[:300]}")
