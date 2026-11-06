"""SQLite storage for the bot's music player.

Keeps the saved songs plus a few settings (delay between lines, channels where
music mode is on). Only uses Python's built-in sqlite3, so nothing new to install.

The database file is music.db next to this file. To keep it somewhere else, set
the MUSIC_DB_PATH environment variable (for example to a Render persistent disk).
"""

import json
import os
import sqlite3
import threading
import time

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "music.db")
DEFAULT_RATE = 2.0


class MusicStore:
    def __init__(self, path=None):
        self.path = path or os.environ.get("MUSIC_DB_PATH") or DEFAULT_DB
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS songs (
                    num        INTEGER PRIMARY KEY,
                    title      TEXT NOT NULL,
                    lines      TEXT NOT NULL,      -- JSON list of lines
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            self._db.commit()

    # -- songs ----------------------------------------------------------------

    def add_song(self, num, title, lines):
        """Save a song. If the number already exists it is replaced."""
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO songs (num, title, lines, created_at) VALUES (?, ?, ?, ?)",
                (int(num), title, json.dumps(list(lines)), time.time()),
            )
            self._db.commit()

    def get_song(self, num):
        """Return {"num", "title", "lines"} or None."""
        with self._lock:
            row = self._db.execute(
                "SELECT num, title, lines FROM songs WHERE num = ?", (int(num),)
            ).fetchone()
        if not row:
            return None
        return {"num": row[0], "title": row[1], "lines": json.loads(row[2])}

    def list_songs(self):
        """Return [(num, title), ...] ordered by number."""
        with self._lock:
            return self._db.execute("SELECT num, title FROM songs ORDER BY num").fetchall()

    def delete_song(self, num):
        """Delete a song. Returns True if it existed."""
        with self._lock:
            cur = self._db.execute("DELETE FROM songs WHERE num = ?", (int(num),))
            self._db.commit()
            return cur.rowcount > 0

    # -- settings -----------------------------------------------------------------

    def _get_setting(self, key, default=None):
        with self._lock:
            row = self._db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def _set_setting(self, key, value):
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value))
            )
            self._db.commit()

    def get_rate(self):
        """Seconds between lines while a song plays (default 2)."""
        try:
            return float(self._get_setting("mrate", DEFAULT_RATE))
        except ValueError:
            return DEFAULT_RATE

    def set_rate(self, seconds):
        self._set_setting("mrate", float(seconds))

    def get_channels(self):
        """Channels (lowercase) where music mode is on."""
        try:
            return set(json.loads(self._get_setting("music_channels", "[]")))
        except ValueError:
            return set()

    def set_channels(self, channels):
        self._set_setting("music_channels", json.dumps(sorted(channels)))
