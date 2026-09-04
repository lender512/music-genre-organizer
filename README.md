# music-genre-organizer

Fetch your saved music library from the command line and organize it by
genre, using per-track tags from Last.fm.

## Setup

1. Create an app on your music provider's developer dashboard and add a
   redirect URI (e.g. `http://127.0.0.1:8888/callback`) to its settings.
2. Copy the example config and fill in your credentials:
   ```bash
   cp config.example.yml config.yml
   ```
3. (Optional, for per-song genres) Get a free Last.fm API key at
   https://www.last.fm/api/account/create and add it under `lastfm.api_key`
   in `config.yml`.
4. Sync dependencies (creates `.venv`):
   ```bash
   uv sync
   ```

## Usage

```bash
uv run music-genre-organizer                    # print all saved songs + genres
uv run music-genre-organizer --limit 50         # only the first 50
uv run music-genre-organizer --json out.json    # also dump full data to JSON
uv run music-genre-organizer --top-tags 5       # up to 5 genre tags per song
uv run music-genre-organizer --no-lastfm        # use artist-level genres only
uv run music-genre-organizer --config other.yml # use a different config file
```

Genres come from Last.fm track tags when a key is configured, falling back to
artist-level genres otherwise. On first run a browser opens for OAuth
authorization; the token is cached (default `.cache`) for later runs.

`config.yml`, `.cache`, and `*.json` are git-ignored so credentials, tokens,
and exported data are never committed.
