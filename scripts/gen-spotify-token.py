#!/usr/bin/env python3
"""Mint SPOTIFY_REFRESH_TOKEN via Spotify authorization-code flow.

Reads SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET from .env (or environment),
opens a throwaway loopback server, and prints the refresh token.

Before running, add this exact redirect URI in the Spotify dashboard:
    http://127.0.0.1:8888/callback

Usage:
    python3 scripts/gen-spotify-token.py [--env-file .env]
"""
from __future__ import annotations

import argparse
import base64
import secrets
import threading
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

REDIRECT_URI = "http://127.0.0.1:8888/callback"
PORT = 8888
SCOPES = "user-library-read playlist-read-private playlist-read-collaborative playlist-modify-public playlist-modify-private"


def read_env_file(path: str) -> dict:
    vals: dict = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip("\"'")
    except FileNotFoundError:
        pass
    return vals


def main() -> int:
    import os

    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default=".env")
    args = ap.parse_args()

    file_vals = read_env_file(args.env_file)
    cid = os.environ.get("SPOTIFY_CLIENT_ID") or file_vals.get("SPOTIFY_CLIENT_ID", "")
    secret = os.environ.get("SPOTIFY_CLIENT_SECRET") or file_vals.get("SPOTIFY_CLIENT_SECRET", "")
    if not cid or not secret:
        print("Missing SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET (env or .env).")
        return 1

    state = secrets.token_hex(16)
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": cid,
        "scope": SCOPES,
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "show_dialog": "true",
    })
    print(f"Listening on {REDIRECT_URI}")
    print("\nOpen this URL in the browser logged into the Spotify account:\n")
    print(f"https://accounts.spotify.com/authorize?{params}\n")
    try:
        webbrowser.open(f"https://accounts.spotify.com/authorize?{params}")
    except Exception:
        pass

    captured: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if self.path.startswith("/callback"):
                if qs.get("state", [""])[0] != state:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"State mismatch.")
                    return
                if qs.get("error"):
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(f"Auth failed: {qs['error'][0]}".encode())
                    return
                captured["code"] = qs.get("code", [""])[0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Done. Refresh token is in your terminal; close this tab.")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", PORT), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        while "code" not in captured:
            t.join(0.2)
    except KeyboardInterrupt:
        return 1
    finally:
        httpd.shutdown()

    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": captured["code"],
        "redirect_uri": REDIRECT_URI,
    }).encode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token", data=data,
        headers={"Authorization": f"Basic {basic}"}, method="POST")
    import json

    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    print("\nGranted scopes:", body.get("scope"))
    print("\nPaste into .env:\n")
    print(f"SPOTIFY_REFRESH_TOKEN={body['refresh_token']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
