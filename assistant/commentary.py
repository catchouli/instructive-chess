"""What the narrators said while a position was on the board, across every
video that reaches it.

    scanner/.venv/bin/python assistant/commentary.py "1.d4 d5 2.Bf4 Nf6" [--catalog PATH]

For each visit to the position (any move order), prints the transcript
segments from shortly before the move that made it (commentary often
comes just before: "I'm going to play Nf3 here") until shortly after the
next move, with a link to that moment in the video. Videos with no saved
transcript are listed as such.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import chess

import positions

TRANSCRIPTS = positions.ROOT / "scanner" / "cache" / "transcripts"
BEFORE = 4.0     # seconds before the move that made the position
AFTER = 4.0      # seconds after the next move
MAX_SPAN = 40.0  # a long think: cap the window this long after the move


def board_after(line: str) -> chess.Board:
    board = chess.Board()
    for tok in line.split():
        tok = re.sub(r"^\d+\.(\.\.)?", "", tok)
        if tok:
            board.push_san(tok)
    return board


def window(v: positions.Visit, from_start: bool = False) -> tuple[float, float]:
    """The position's own time on the board, or with `from_start` everything
    from the game's first move (openings are often blitzed out in seconds,
    and the commentary runs across the whole sequence)."""
    end = v.seconds + MAX_SPAN if v.until is None else min(v.until, v.seconds + MAX_SPAN)
    return (v.start if from_start else v.seconds) - BEFORE, end + AFTER


def segments(video_id: str) -> list[dict] | None:
    path = TRANSCRIPTS / f"{video_id}.json"
    return json.loads(path.read_text())["segments"] if path.exists() else None


def said(v: positions.Visit, segs: list[dict], from_start: bool = False) -> str:
    lo, hi = window(v, from_start)
    return " ".join(s["text"] for s in segs if s["end"] > lo and s["start"] < hi)


def stamp(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("line", help='moves reaching the position, e.g. "1.d4 d5 2.Bf4"')
    ap.add_argument("--catalog", type=Path, default=positions.CATALOG)
    ap.add_argument("--from-start", action="store_true", help="include everything said since the game's first move")
    ap.add_argument("--channel", help="only this narrator")
    args = ap.parse_args(argv)
    key = positions.key_of(board_after(args.line))
    vs = sorted(positions.load(args.catalog).get(key, []), key=lambda v: (v.channel, v.video_id, v.seconds))
    if args.channel:
        vs = [v for v in vs if v.channel == args.channel]
    print(f"{key}\n{len(vs)} visits in {len({v.video_id for v in vs})} videos\n")
    cache: dict[str, list[dict] | None] = {}
    for v in vs:
        if v.video_id not in cache:
            cache[v.video_id] = segments(v.video_id)
        segs = cache[v.video_id]
        lo, _ = window(v, args.from_start)
        url = f"https://youtu.be/{v.video_id}?t={max(0, int(lo))}"
        print(f"[{v.channel}] {v.game_id} at {stamp(v.seconds)}  {url}")
        tail = max(0, len(v.line) - 4)
        print(f"   via {'... ' if tail else ''}{positions.line_san(v.line[tail:], tail)}")
        print(f"   {said(v, segs, args.from_start) if segs else '(no transcript)'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
