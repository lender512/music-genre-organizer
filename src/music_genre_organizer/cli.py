"""Fetch and print your Spotify "Liked Songs" (saved tracks).

Run via the console script:
    uv run music-genre-organizer                    # print all liked songs
    uv run music-genre-organizer --limit 50         # only fetch the first 50
    uv run music-genre-organizer --json out.json    # also dump full data to JSON
    uv run music-genre-organizer --config other.yml # use a different config file

Auth (OAuth Authorization Code flow):
    Copy config.example.yml to config.yml and fill in the values. Create an
    app at https://developer.spotify.com/dashboard and add the redirect_uri
    there. On first run a browser opens for you to authorize; a token is then
    cached (see cache_path) for subsequent runs.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import spotipy
import yaml
from spotipy.oauth2 import SpotifyOAuth

# Only need read access to the user's saved tracks.
SCOPE = "user-library-read"
PAGE_SIZE = 50  # Spotify API max page size for saved tracks.
DEFAULT_CONFIG = "config.yml"

LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"
LASTFM_WORKERS = 5  # concurrent requests; Last.fm tolerates ~5 req/s per key.
DEFAULT_TOP_TAGS = 3


def load_config(path: str) -> dict:
    """Load and validate the YAML config file."""
    cfg_path = Path(path)
    if not cfg_path.is_file():
        raise FileNotFoundError(
            f"Config file '{path}' not found. Copy config.example.yml to "
            f"{DEFAULT_CONFIG} and fill in your Spotify credentials."
        )

    with cfg_path.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}

    spotify = cfg.get("spotify") or {}
    required = ("client_id", "client_secret", "redirect_uri")
    missing = [k for k in required if not spotify.get(k)]
    if missing:
        raise ValueError(
            f"Missing required keys under 'spotify' in {path}: "
            f"{', '.join(missing)}"
        )
    return cfg


def get_client(cfg: dict) -> spotipy.Spotify:
    """Build an authenticated Spotify client from config values."""
    spotify = cfg["spotify"]
    auth = SpotifyOAuth(
        client_id=spotify["client_id"],
        client_secret=spotify["client_secret"],
        redirect_uri=spotify["redirect_uri"],
        scope=SCOPE,
        cache_path=spotify.get("cache_path", ".cache"),
        open_browser=True,
    )
    return spotipy.Spotify(auth_manager=auth, requests_timeout=30, retries=3)


def fetch_liked(sp: spotipy.Spotify, limit: int | None) -> list[dict]:
    """Page through saved tracks, returning a list of simplified dicts."""
    songs: list[dict] = []
    offset = 0

    while True:
        page_limit = PAGE_SIZE
        if limit is not None:
            remaining = limit - len(songs)
            if remaining <= 0:
                break
            page_limit = min(PAGE_SIZE, remaining)

        results = sp.current_user_saved_tracks(limit=page_limit, offset=offset)
        items = results.get("items", [])
        if not items:
            break

        for item in items:
            track = item.get("track") or {}
            track_artists = track.get("artists", [])
            songs.append(
                {
                    "added_at": item.get("added_at"),
                    "name": track.get("name"),
                    "artists": [a.get("name") for a in track_artists],
                    "artist_ids": [a.get("id") for a in track_artists if a.get("id")],
                    "album": (track.get("album") or {}).get("name"),
                    "duration_ms": track.get("duration_ms"),
                    "url": (track.get("external_urls") or {}).get("spotify"),
                    "id": track.get("id"),
                    # Filled in later by enrich_genres(); genres live on the
                    # artist object, not the track.
                    "genres": [],
                }
            )

        offset += len(items)
        if results.get("next") is None:
            break
        time.sleep(0.05)  # be gentle with the API

    return songs


ARTIST_BATCH = 50  # Spotify API max artists per request.


def enrich_genres(sp: spotipy.Spotify, songs: list[dict]) -> None:
    """Attach genres to each song in place, based on its artists.

    Spotify exposes genres on the artist object, so we collect every unique
    artist id, fetch them in batches of 50, then union each track's artists'
    genres back onto the song.
    """
    # Unique artist ids across all songs.
    artist_ids = {aid for s in songs for aid in s["artist_ids"]}
    if not artist_ids:
        return

    genre_by_artist: dict[str, list[str]] = {}
    ids = list(artist_ids)
    for start in range(0, len(ids), ARTIST_BATCH):
        batch = ids[start : start + ARTIST_BATCH]
        resp = sp.artists(batch)
        for artist in resp.get("artists", []):
            if artist and artist.get("id"):
                genre_by_artist[artist["id"]] = artist.get("genres", [])
        time.sleep(0.05)  # be gentle with the API

    for s in songs:
        genres: list[str] = []
        for aid in s["artist_ids"]:
            for g in genre_by_artist.get(aid, []):
                if g not in genres:
                    genres.append(g)
        s["genres"] = genres


def _fetch_lastfm_tags(
    session: requests.Session, api_key: str, artist: str, track: str, top_n: int
) -> list[str]:
    """Return up to ``top_n`` Last.fm top tags for a single track.

    Uses the ``track.getTopTags`` method. Tags are community-generated and
    already ranked by count, so we just take the highest-weighted ones.
    Returns an empty list on any error or when nothing matches.
    """
    if not artist or not track:
        return []

    params = {
        "method": "track.gettoptags",
        "artist": artist,
        "track": track,
        "api_key": api_key,
        "format": "json",
        "autocorrect": 1,
    }
    try:
        resp = session.get(LASTFM_API_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return []

    # Last.fm returns {"toptags": {"tag": [{"name": ..., "count": ...}, ...]}}.
    # On a lookup miss it may instead return an {"error": ...} payload.
    tags = (data.get("toptags") or {}).get("tag", [])
    if isinstance(tags, dict):  # single-tag responses come back as a dict
        tags = [tags]
    names = [t.get("name", "").lower() for t in tags if t.get("name")]
    return names[:top_n]


def enrich_lastfm(api_key: str, songs: list[dict], top_n: int) -> int:
    """Attach per-song Last.fm tags in place. Returns count of tagged songs.

    Fetches tags concurrently (one request per track). Results are cached by
    (artist, track) so duplicate tracks aren't fetched twice.
    """
    cache: dict[tuple[str, str], list[str]] = {}

    def worker(song: dict) -> list[str]:
        artist = song["artists"][0] if song["artists"] else ""
        track = song["name"] or ""
        key = (artist.lower(), track.lower())
        if key not in cache:
            with requests.Session() as session:
                cache[key] = _fetch_lastfm_tags(
                    session, api_key, artist, track, top_n
                )
        return cache[key]

    with ThreadPoolExecutor(max_workers=LASTFM_WORKERS) as pool:
        results = list(pool.map(worker, songs))

    tagged = 0
    for song, tags in zip(songs, results):
        song["lastfm_tags"] = tags
        if tags:
            tagged += 1
    return tagged


def resolve_genres(songs: list[dict]) -> None:
    """Set each song's display ``genres`` to Last.fm tags, falling back to
    artist genres when the track had no Last.fm tags."""
    for s in songs:
        if s.get("lastfm_tags"):
            s["genres"] = s["lastfm_tags"]
        # else: keep the artist-level genres already set by enrich_genres()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Query your Spotify liked songs.")
    p.add_argument("--config", default=DEFAULT_CONFIG, help="path to config YAML")
    p.add_argument("--limit", type=int, default=None, help="max songs to fetch")
    p.add_argument("--json", dest="json_path", help="write full results to this JSON file")
    p.add_argument(
        "--top-tags",
        type=int,
        default=DEFAULT_TOP_TAGS,
        help=f"max Last.fm tags per song (default {DEFAULT_TOP_TAGS})",
    )
    p.add_argument(
        "--no-lastfm",
        action="store_true",
        help="skip Last.fm; use Spotify artist genres only",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        cfg = load_config(args.config)
        sp = get_client(cfg)
        songs = fetch_liked(sp, args.limit)
        enrich_genres(sp, songs)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    except spotipy.SpotifyOauthError as exc:
        print(f"Auth error: {exc}", file=sys.stderr)
        return 1
    except spotipy.SpotifyException as exc:
        print(f"Spotify API error: {exc}", file=sys.stderr)
        return 1

    # Per-song genres via Last.fm, with Spotify artist genres as fallback.
    lastfm_key = (cfg.get("lastfm") or {}).get("api_key")
    if not args.no_lastfm and lastfm_key:
        tagged = enrich_lastfm(lastfm_key, songs, args.top_tags)
        resolve_genres(songs)
        print(
            f"Last.fm tags found for {tagged}/{len(songs)} songs "
            f"(rest fall back to artist genres).",
            file=sys.stderr,
        )
    elif not args.no_lastfm and not lastfm_key:
        print(
            "No 'lastfm.api_key' in config; using Spotify artist genres only. "
            "Add a key (https://www.last.fm/api/account/create) for per-song genres.",
            file=sys.stderr,
        )

    for i, s in enumerate(songs, 1):
        artists = ", ".join(a for a in s["artists"] if a)
        genres = ", ".join(s["genres"]) if s["genres"] else "—"
        print(f"{i:>4}. {s['name']} — {artists}  [{s['album']}]  ({genres})")

    print(f"\nTotal liked songs: {len(songs)}", file=sys.stderr)

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(songs, fh, ensure_ascii=False, indent=2)
        print(f"Wrote {len(songs)} songs to {args.json_path}", file=sys.stderr)

    return 0
