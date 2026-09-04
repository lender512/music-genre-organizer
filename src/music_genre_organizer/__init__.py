"""music-genre-organizer: organize your saved music library by genre."""

import sys

from music_genre_organizer.cli import main as _main


def main() -> None:
    """Console-script entry point."""
    sys.exit(_main())


__all__ = ["main"]
