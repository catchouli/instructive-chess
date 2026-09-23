"""Merge scanned games into the static web app's data file (web/data/games.json)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from download import Source
import trust
from reconstruct import Game

DATA_FILE = Path(__file__).resolve().parents[1] / "web" / "data" / "games.json"


def _clock_seconds(values: list[float | None]) -> list[int | None]:
    return [None if v is None else int(round(v)) for v in values]


def add_trust(entry: dict) -> None:
    """Per-move confidence and how many plies the web app should show (see
    trust.py). Every move stays in the data either way."""
    entry["move_confidence"], entry["trusted_ply"] = trust.assess(entry["moves_san"], entry["move_seconds"])


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
        "flipped": game.flipped,
        "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    add_trust(entry)
    # Only present when clock reading found a usable clock for this game -
    # older/unread entries simply omit these rather than carry an all-null
    # array, so the web app can tell "not attempted" from "attempted, empty".
    if game.clock_white_seconds:
        entry["clock_white_seconds"] = _clock_seconds(game.clock_white_seconds)
    if game.clock_black_seconds:
        entry["clock_black_seconds"] = _clock_seconds(game.clock_black_seconds)
    # Same "only present when found" convention as the clock fields above -
    # lets the web app tell "not attempted" from "attempted, not confident".
    if game.rating_white is not None:
        entry["rating_white"] = game.rating_white
    if game.rating_black is not None:
        entry["rating_black"] = game.rating_black
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
    # Write to a temp file and rename over the target rather than writing
    # in place - the web app polls this exact file live, and an in-place
    # write is visible mid-truncation to a concurrent reader (confirmed:
    # the static server's response briefly 200s with an empty/partial body
    # during a write), which app.js's fetch sees as a JSON parse failure
    # and reports as "no games in the catalog" even though nothing is
    # actually wrong. os.replace is atomic on the same filesystem, so a
    # reader always sees either the old file or the new one, never neither.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(combined, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)
    return len(fresh), len(combined)
