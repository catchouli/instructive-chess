"""Merge scanned games into the static web app's data file (web/data/games.json)."""
from __future__ import annotations

import argparse
import json
import os
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
    """The fields the web app filters on besides the moves:

    - `pov`: the colour the narrator (the channel's owner) played - the
      "instructive point of view". A player's own live board shows their
      side at the bottom, so a board read flipped means the narrator was
      black. Verified against the real games' player names by
      tests/benchmark.py (the "narrator side" line).
    - `speedrun`: see is_speedrun.
    - `tags`: the kind of video (see video_kind) and the time control (see
      time_control), when known - e.g. ["speedrun", "blitz"].
    """
    entry["pov"] = "black" if entry.get("flipped") else "white"
    entry["speedrun"] = is_speedrun(entry.get("video_title", "")) if speedrun is None else bool(speedrun)
    entry["tags"] = [t for t in (video_kind(entry), time_control(entry)) if t]


CACHE = Path(__file__).resolve().parent / "cache"


def video_kind(entry: dict) -> str:
    """"speedrun", "stream" (a past live stream, from YouTube's metadata) or
    "video" (recorded and edited)."""
    if entry.get("speedrun"):
        return "speedrun"
    try:
        info = json.loads((CACHE / f"{entry['video_id']}.info.json").read_text())
    except (FileNotFoundError, KeyError, ValueError):
        return "video"
    return "stream" if info.get("was_live") or info.get("live_status") == "was_live" else "video"


def time_control(entry: dict) -> str | None:
    """"bullet" (under ~3 min), "blitz" (up to 5), "rapid" (up to 15) or
    "classical", from the clocks at the start of the game, or None without
    them. Each side's starting time is the median of its first readings (one
    misread can't decide it). Where both sides were read they must agree:
    on arena streams the tournament's countdown has been read as a player's
    clock (all 10 "classical" games in the catalog of 2026-10-22). One side
    alone is believed up to rapid. A lichess arena game the narrator
    berserked starts at half time (3+0 at 90 s) and counts as played: bullet."""
    def start(key):
        vals = sorted(v for v in (entry.get(key) or [])[:8] if v is not None)
        return vals[len(vals) // 2] if len(vals) >= 2 else None
    w, b = start("clock_white_seconds"), start("clock_black_seconds")
    if w is not None and b is not None:
        if max(w, b) > 1.25 * min(w, b):
            return None
        t = max(w, b)
    else:
        t = w if w is not None else b
        if t is None or t > 930:
            return None
    # a 3-minute game reads 179-184 s after its first moves (a second gone, or the increment)
    return "bullet" if t < 175 else "blitz" if t <= 330 else "rapid" if t <= 930 else "classical"


def _clock_seconds(values: list[float | None]) -> list[int | None]:
    return [None if v is None else int(round(v)) for v in values]


# On after the 2026-10-20 rebuild (handoff/full1), which is merged with
# drop_final_bunch applied afterwards; off while it runs so it stays one version.
DROP_FINAL_BUNCH = os.environ.get("CATALOG_DROP_FINAL_BUNCH", "1") == "1"
_PLY_MARKS = ("ambiguous_plies", "fuzzy_plies", "doubtful_plies", "rough_plies")
DROP_UNSETTLED = int(os.environ.get("CATALOG_DROP_UNSETTLED", "10"))   # 0 = off


def _truncate(entry: dict, start: int) -> None:
    """Keep only the first *start* plies of an entry, with its marks."""
    for key in ("moves_san", "move_seconds", "clock_white_seconds", "clock_black_seconds", "move_confidence"):
        if key in entry:
            entry[key] = entry[key][:start]
    for key in _PLY_MARKS:
        if key in entry:
            kept = [i for i in entry[key] if i < start]
            if kept:
                entry[key] = kept
            else:
                del entry[key]
    entry["ply"] = start
    entry["pgn"] = " ".join(f"{i // 2 + 1}. {san}" if i % 2 == 0 else san for i, san in enumerate(entry["moves_san"]))


def drop_final_bunch(entry: dict) -> int:
    """Drop a game's last plies when they arrived together as one bunch (2+
    plies at one timestamp). Measured on 1,620 benchmarked games: a quarter
    of the games with any wrong move were wrong only in their last 1-2
    plies, mostly such a bunch - the narrator showing a line once the game
    was over, or the video moving on mid-game. Dropping them: every move
    right 979 -> 1,107 games, for 464 right plies that trust held back
    anyway (shown 1,020 -> 1,019). Returns how many plies were dropped;
    call add_trust afterwards."""
    times = entry["move_seconds"]
    n = len(times)
    start = n
    while start > 0 and times[start - 1] == times[-1]:
        start -= 1
    if n - start < 2 or start < 10:
        return 0
    _truncate(entry, start)
    return n - start


def drop_unsettled_tail(entry: dict, settled: list[bool]) -> int:
    """Drop up to DROP_UNSETTLED of a game's final plies whose positions were
    never read again before the next ply (reconstruct.Game.settled): where a
    game ends with them, reconstruction had usually derailed - a wrong ply
    accepted, nothing after it explained. Measured on the 1,656 matched games
    of the whole library (reconstruct only): every move right 61.0% -> 78.7%,
    for 1.1% of right plies (all of them untrusted). Keeps at least 10 plies.
    Returns how many were dropped; call add_trust afterwards."""
    n = start = len(entry["moves_san"])
    while start > 10 and n - start < DROP_UNSETTLED and start <= len(settled) and not settled[start - 1]:
        start -= 1
    if start < n:
        _truncate(entry, start)
    return n - start


def add_trust(entry: dict) -> None:
    """Per-move confidence and how many plies the web app should show (see
    trust.py). Every move stays in the data either way."""
    both_clocks = bool(entry.get("clock_white_seconds")) and bool(entry.get("clock_black_seconds"))
    entry["move_confidence"], entry["trusted_ply"] = trust.assess(entry["moves_san"], entry["move_seconds"],
                                                                  clocks_checked_end=both_clocks,
                                                                  ambiguous=entry.get("ambiguous_plies", ()),
                                                                  fuzzy=entry.get("fuzzy_plies", ()),
                                                                  doubtful=entry.get("doubtful_plies", ()),
                                                                  rough=entry.get("rough_plies", ()))


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
        # Both sides the same value: one rating read twice (6 of 337
        # benchmarked games, the opponent's wrong in all six) - the
        # narrator's is the one that agrees with the rest of the video.
        if e.get(theirs) is not None and e.get(theirs) == e.get(mine):
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
    if game.ambiguous:
        entry["ambiguous_plies"] = list(game.ambiguous)   # see reconstruct.Game
    if game.fuzzy:
        entry["fuzzy_plies"] = list(game.fuzzy)
    if game.doubtful:
        entry["doubtful_plies"] = list(game.doubtful)
    if game.rough:
        entry["rough_plies"] = list(game.rough)
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
    if DROP_FINAL_BUNCH:
        drop_final_bunch(entry)
    if DROP_UNSETTLED:
        drop_unsettled_tail(entry, game.settled)
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
    """Re-derive the `pov` and `speedrun` tags, the per-move trust (trust.py)
    and blank implausible ratings (see reconcile_ratings) for every entry of
    a catalog - all from fields already stored, so no rescan is needed:

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
        add_trust(e)
        by_video.setdefault(e["video_id"], []).append(e)
    blanked = sum(reconcile_ratings(v) for v in by_video.values())
    write(entries, args.catalog)
    n_speed = sum(e["speedrun"] for e in entries)
    n_black = sum(e["pov"] == "black" for e in entries)
    tags: dict[str, int] = {}
    for e in entries:
        for t in e["tags"]:
            tags[t] = tags.get(t, 0) + 1
    print(f"tagged {len(entries)} games in {args.catalog}: {n_speed} speedrun, "
          f"narrator black in {n_black}, {blanked} implausible rating(s) blanked; tags {tags}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
