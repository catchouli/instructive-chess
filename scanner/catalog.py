"""Merge scanned games into the static web app's data file (web/data/games.json)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from download import Source
from reconstruct import Game

DATA_FILE = Path(__file__).resolve().parents[1] / "web" / "data" / "games.json"


def _clock_seconds(values: list[float | None]) -> list[int | None]:
    return [None if v is None else int(round(v)) for v in values]


def _entry(source: Source, game: Game, index: int) -> dict:
    entry = {
        "id": f"{source.video_id}-{index + 1}",
        "video_id": source.video_id,
        "video_url": source.url,
        "video_title": source.title,
        "channel": source.channel,
        "published": source.published,
        "start_seconds": int(round(game.start_seconds)),
        "moves_san": list(game.moves_san),
        "move_seconds": [int(round(t)) for t in game.move_seconds],
        "pgn": game.pgn(),
        "ply": game.ply,
        "confidence": round(game.confidence, 3),
        "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    # Only present when clock reading found a usable clock for this game -
    # older/unread entries simply omit these rather than carry an all-null
    # array, so the web app can tell "not attempted" from "attempted, empty".
    if game.clock_white_seconds:
        entry["clock_white_seconds"] = _clock_seconds(game.clock_white_seconds)
    if game.clock_black_seconds:
        entry["clock_black_seconds"] = _clock_seconds(game.clock_black_seconds)
    return entry


def _sort_key(e: dict):
    # newest first; "" published dates sort last
    return (e.get("published") or "0000-00-00", -e.get("start_seconds", 0))


def build_entries(source: Source, games: list[Game]) -> list[dict]:
    return [_entry(source, g, i) for i, g in enumerate(games)]


def load(path: Path = DATA_FILE) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def merge(source: Source, games: list[Game], path: Path = DATA_FILE) -> tuple[int, int]:
    """Replace this video's entries with freshly scanned ones. Returns (added, total)."""
    existing = [e for e in load(path) if e.get("video_id") != source.video_id]
    fresh = build_entries(source, games)
    combined = existing + fresh
    combined.sort(key=lambda e: _sort_key(e), reverse=True)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(combined, indent=2, ensure_ascii=False) + "\n")
    return len(fresh), len(combined)
