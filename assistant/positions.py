"""Every position reached in a catalog's trusted moves, keyed so that
transpositions meet: the EPD of the board (placement, side to move,
castling, en passant only when it can be taken), without move counters.

    scanner/.venv/bin/python assistant/positions.py [--catalog PATH] [--top N] [--min-ply P]

prints the positions reached in the most videos (then by most narrators),
with one line that reaches each. Only trusted plies are used: the
assistant should never learn from a move the scanner can't vouch for.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import chess

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "web" / "data" / "games.json"


@dataclass
class Visit:
    """A position on the board in one game: from the move that made it
    (`seconds` into the video) until the next move (`until`)."""
    key: str
    game_id: str
    video_id: str
    channel: str
    ply: int              # plies played to reach it
    line: list[str]       # the moves that reached it, in this game
    seconds: float
    until: float | None   # None when it's the last trusted position
    start: float          # the game's first move


def key_of(board: chess.Board) -> str:
    return board.epd()


def visits(entry: dict) -> list[Visit]:
    """The positions after each trusted ply of one catalog game (the start
    position is left out - every game has it)."""
    moves, times = entry["moves_san"], entry["move_seconds"]
    trusted = min(entry.get("trusted_ply", 0), len(moves))
    board, out = chess.Board(), []
    for i in range(trusted):
        board.push_san(moves[i])
        nxt = times[i + 1] if i + 1 < len(times) else None
        out.append(Visit(key_of(board), entry["id"], entry["video_id"], entry.get("channel") or "",
                         i + 1, moves[:i + 1], times[i], nxt, times[0]))
    return out


def load(catalog: Path) -> dict[str, list[Visit]]:
    by_key: dict[str, list[Visit]] = defaultdict(list)
    for entry in json.loads(catalog.read_text()):
        for v in visits(entry):
            by_key[v.key].append(v)
    return by_key


def line_san(line: list[str], first_ply: int = 0) -> str:
    """Moves in PGN style; `first_ply` numbers a tail of a game correctly."""
    out = []
    for i, m in enumerate(line, first_ply):
        if i % 2 == 0:
            out.append(f"{i // 2 + 1}.{m}")
        else:
            out.append(m if out else f"{i // 2 + 1}...{m}")
    return " ".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalog", type=Path, default=CATALOG)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--min-ply", type=int, default=8, help="skip positions reached in fewer plies")
    args = ap.parse_args(argv)
    by_key = load(args.catalog)
    rows = []
    for key, vs in by_key.items():
        ply = min(v.ply for v in vs)
        if ply < args.min_ply:
            continue
        vids = {v.video_id for v in vs}
        chans = {v.channel for v in vs}
        rows.append((len(vids), len(chans), len(vs), ply, key, vs))
    rows.sort(key=lambda r: (r[0], r[1], r[2]), reverse=True)
    print(f"{len(by_key)} distinct positions; {len(rows)} at ply {args.min_ply}+")
    for n_vid, n_chan, n_games, ply, key, vs in rows[:args.top]:
        shortest = min(vs, key=lambda v: v.ply)
        print(f"{n_vid:4d} videos {n_chan} narrators {n_games:4d} games  {line_san(shortest.line)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
