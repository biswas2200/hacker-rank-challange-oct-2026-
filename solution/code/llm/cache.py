"""ResponseCache: model answers saved on disk, keyed by what was asked.

A second run with the same inputs reads the saved answer instead of calling
Gemini, so it costs nothing, works offline (--no-llm) and gives the same
output.csv even if the hosted model changes. Each entry also keeps the token
counts of the call that produced it, so usage_report.md can state what the
answers in the final run cost.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def cache_key(*parts: bytes | str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8") if isinstance(part, str) else part)
        digest.update(b"\x00")  # keeps ("ab", "c") distinct from ("a", "bc")
    return digest.hexdigest()


class ResponseCache:
    def __init__(self, directory: Path):
        self.directory = directory

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str) -> dict | None:
        path = self._path(key)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def put(self, key: str, entry: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._path(key).write_text(json.dumps(entry, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
