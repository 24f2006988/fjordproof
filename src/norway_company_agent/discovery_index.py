"""Declared website discovery cache built offline from Common Crawl (scripts/build_discovery_index.py).

It only names candidate hosts and the page where the crawl saw the organisation number. Nothing in it is
evidence: footprint_plus publishes a site only when the exact organisation number is printed on it live.
Turn it off with SIGNALPOST_DISCOVERY_CACHE=off.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "discovery_index.json.gz"
_LOCK = threading.Lock()
_PAIRS: dict[str, list[list[str]]] | None = None


def enabled() -> bool:
    return os.environ.get("SIGNALPOST_DISCOVERY_CACHE", "on").strip().casefold() not in {"off", "0", "false", "no"}


def _load() -> dict[str, list[list[str]]]:
    global _PAIRS
    with _LOCK:
        if _PAIRS is None:
            path = Path(os.environ.get("SIGNALPOST_DISCOVERY_INDEX") or DEFAULT_PATH)
            try:
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    _PAIRS = json.load(fh).get("pairs") or {}
            except (OSError, ValueError):
                _PAIRS = {}
        return _PAIRS


def reset() -> None:
    """Forget the loaded index (tests)."""
    global _PAIRS
    with _LOCK:
        _PAIRS = None


def candidates(profile: dict[str, Any]) -> list[tuple[str, str]]:
    """(host, page_url) pairs for this organisation number, or [] when the cache is off or has no entry."""
    if not enabled():
        return []
    org = re.sub(r"\D", "", str(profile.get("organisation_number") or ""))
    if len(org) != 9:
        return []
    return [(host, url) for host, url in _load().get(org, [])][:3]
