"""Merge scanned games into the static web app's data file (web/data/games.json)."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from download import Source
import trust
from reconstruct import Game

DATA_FILE = Path(__file__).resolve().parents[1] / "web" / "data" / "games.json"

# A speedrun: the narrator plays a fresh low-rated account up the ladder, so
# the opponents are weak and only the narrator's side of each game is
# instructive. Recognised from the video title; scan.py --speedrun /
# --no-speedrun overrides for a video this misses.
_SPEEDRUN_TITLE = re.compile(r"speed ?run|beginner to master|climbing the rating ladder|rating climb", re.I)


def is_speedrun(title: str) -> bool:
    return bool(_SPEEDRUN_TITLE.search(title or ""))


def tag(entry: dict, speedrun: bool | None = None) -> None:
    """The two fields the web app filters on besides the moves:

    - `pov`: the colour the narrator (the channel's owner) played - the
      "instructive point of view". A player's own live board shows their
      side at the bottom, so a board read flipped means the narrator was
      black. Verified against the real games' player names by
      tests/benchmark.py (the "narrator side" line).
    - `speedrun`: see is_speedrun.
    """
    entry["pov"] = "black" if entry.get("flipped") else "white"
    entry["speedrun"] = is_speedrun(entry.get("video_title", "")) if speedrun is None else bool(speedrun)


def _clock_seconds(values: list[float | None]) -> list[int | None]:
    return [None if v is None else int(round(v)) for v in values]


def add_trust(entry: dict) -> None:
    """Per-move confidence and how many plies the web app should show (see
    trust.py). Every move stays in the data either way."""
    both_clocks = bool(entry.get("clock_white_seconds")) and bool(entry.get("clock_black_seconds"))
    entry["move_confidence"], entry["trusted_ply"] = trust.assess(entry["moves_san"], entry["move_seconds"],
                                                                  clocks_checked_end=both_clocks)


NARRATOR_RATING_SPREAD = 250   # a narrator's rating moves less than this within one video
TITLED_LEVEL = 1500            # a narrator rated this high isn't paired with...
OPPONENT_FLOOR = 1000          # ...an opponent under this in a rated pool


def reconcile_ratings(entries: list[dict]) -> int:
    """Cross-check the ratings read for one *video's* games against each
    other, blanking the implausible ones (a missing rating is harmless, a
    wrong one isn't). Uses the narrator's side (`pov`): their rating is the
    same account all video long, so a read far from the video's median is a
    misread (a 2755 read as 155 or 172 - digits lost to blur). And a
    narrator at titled level isn't paired with a sub-1000 opponent, so such
    a read is a misread too. A speedrun's median is low, so the opponent
    rule doesn't fire there. Returns how many values were blanked."""
    def narrator(e):
        return e.get("rating_white") if e.get("pov") == "white" else e.get("rating_black")

    values = sorted(v for v in (narrator(e) for e in entries) if v is not None)
    if len(values) < 3:
        return 0
    # Misreads lose digits (2747 -> 762, 192...), never gain them, so on a
    # blurry video the 3-digit junk can outnumber the real reads: when at
    # least two 4-digit reads exist, they define the median.
    strong = [v for v in values if v >= 1000]
    base = strong if len(strong) >= 2 else values
    median = base[len(base) // 2]
    blanked = 0
    for e in entries:
        mine = "rating_white" if e.get("pov") == "white" else "rating_black"
        theirs = "rating_black" if mine == "rating_white" else "rating_white"
        if e.get(mine) is not None and abs(e[mine] - median) > NARRATOR_RATING_SPREAD:
            del e[mine]
            blanked += 1
        if median >= TITLED_LEVEL and e.get(theirs) is not None and e[theirs] < OPPONENT_FLOOR:
            del e[theirs]
            blanked += 1
    return blanked


def _entry(source: Source, game: Game, index: int, speedrun: bool | None = None) -> dict:
    entry = {
        "id": f"{source.video_id}-{index + 1}",
        "video_id": source.video_id,
        "video_url": source.url,
        "video_title": source.title,
        "channel": source.channel,
        "published": source.published,
        "start_seconds": int(round(game.start_seconds)),
        "moves_san": list(game.moves_san),
        # one decimal, not whole seconds: at 2 fps two moves 0.5s apart
        # would otherwise share a time and look confirmed together (see trust.py)
        "move_seconds": [round(t, 1) for t in game.move_seconds],
        "pgn": game.pgn(),
        "ply": game.ply,
        "confidence": round(game.confidence, 3),
        "flipped": game.flipped,
        "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
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
    add_trust(entry)   # last: it looks at the clock fields
    tag(entry, speedrun)
    return entry


def _sort_key(e: dict):
    # newest first; "" published dates sort last
    return (e.get("published") or "0000-00-00", -e.get("start_seconds", 0))


def build_entries(source: Source, games: list[Game], speedrun: bool | None = None) -> list[dict]:
    entries = [_entry(source, g, i, speedrun) for i, g in enumerate(games)]
    reconcile_ratings(entries)
    return entries


def load(path: Path = DATA_FILE) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def merge(source: Source, games: list[Game], path: Path = DATA_FILE,
          speedrun: bool | None = None) -> tuple[int, int]:
    """Replace this video's entries with freshly scanned ones. Returns (added, total)."""
    existing = [e for e in load(path) if e.get("video_id") != source.video_id]
    fresh = build_entries(source, games, speedrun)
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
    write(combined, path)
    return len(fresh), len(combined)


def write(entries: list[dict], path: Path = DATA_FILE) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    """Re-derive the `pov` and `speedrun` tags for every entry of a catalog
    and blank implausible ratings (see reconcile_ratings) - all from fields
    already stored, so no rescan is needed:

        scanner/.venv/bin/python scanner/catalog.py [--catalog PATH] [--speedrun VIDEO_ID ...]
                                                     [--no-speedrun VIDEO_ID ...]
    """
    ap = argparse.ArgumentParser(description=main.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalog", type=Path, default=DATA_FILE)
    ap.add_argument("--speedrun", nargs="*", default=[], help="video IDs to tag as speedruns regardless of title")
    ap.add_argument("--no-speedrun", nargs="*", default=[], help="video IDs to tag as not speedruns")
    args = ap.parse_args(argv)
    entries = load(args.catalog)
    by_video: dict[str, list[dict]] = {}
    for e in entries:
        override = True if e["video_id"] in args.speedrun else False if e["video_id"] in args.no_speedrun else None
        tag(e, override)
        by_video.setdefault(e["video_id"], []).append(e)
    blanked = sum(reconcile_ratings(v) for v in by_video.values())
    write(entries, args.catalog)
    n_speed = sum(e["speedrun"] for e in entries)
    n_black = sum(e["pov"] == "black" for e in entries)
    print(f"tagged {len(entries)} games in {args.catalog}: {n_speed} speedrun, "
          f"narrator black in {n_black}, {blanked} implausible rating(s) blanked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
