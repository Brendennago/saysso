"""Tiny SQLite cache. Re-running a check on the same report costs zero fetches and zero tokens."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


def default_cache_dir() -> Path:
    if os.environ.get("SAYSSO_CACHE_DIR"):
        return Path(os.environ["SAYSSO_CACHE_DIR"]).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "saysso"


class Cache:
    def __init__(self, path: str | Path | None = None, enabled: bool = True):
        self.enabled = enabled
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        if not enabled:
            return
        directory = Path(path).expanduser() if path else default_cache_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(directory / "cache.sqlite3", check_same_thread=False)
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS kv (ns TEXT, key TEXT, value TEXT, ts REAL, PRIMARY KEY (ns, key))"
            )
            self._db.commit()
        except (OSError, sqlite3.Error):
            # Read-only home, sandbox, etc. Run without a cache rather than fail.
            self.enabled = False
            self._db = None

    @staticmethod
    def key(*parts: Any) -> str:
        h = hashlib.sha256()
        for p in parts:
            h.update(str(p).encode("utf-8", "replace"))
            h.update(b"\x1f")
        return h.hexdigest()

    def get(self, ns: str, key: str, max_age: float | None = None) -> Any | None:
        if not self._db:
            return None
        with self._lock:
            row = self._db.execute("SELECT value, ts FROM kv WHERE ns=? AND key=?", (ns, key)).fetchone()
        if not row:
            return None
        value, ts = row
        if max_age is not None and time.time() - ts > max_age:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None

    def set(self, ns: str, key: str, value: Any) -> None:
        if not self._db:
            return
        payload = json.dumps(value, ensure_ascii=False)
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO kv (ns, key, value, ts) VALUES (?, ?, ?, ?)",
                (ns, key, payload, time.time()),
            )
            self._db.commit()

    def close(self) -> None:
        if self._db:
            with self._lock:
                self._db.close()
            self._db = None
