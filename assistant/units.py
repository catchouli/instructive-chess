"""The commentary of each game, cut into units: what was said while each
position was on the board.

    scanner/.venv/bin/python assistant/units.py VIDEO_ID [--catalog PATH]

A transcript segment belongs to the position on the board at its midpoint
(from the move that made it until the next move; the last position runs to
the end of the game's commentary, capped at TAIL seconds). Consecutive
segments under one position make one unit. Units from untrusted plies are
kept but marked, so they can be sorted for measuring and never learnt from.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import positions

TRANSCRIPTS = positions.ROOT / "scanner" / "cache" / "transcripts"
LEAD = 5.0    # seconds before the first move that still belong to the game
TAIL = 30.0   # how long after the last move the final position keeps its commentary


@dataclass
class Unit:
    id: str              # <game_id>:<ply>
    game_id: str
    video_id: str
    channel: str
    ply: int             # plies played (0: before the first move)
    last_move: str       # SAN of the move that made the position, "" at ply 0
    recent: str          # the last few moves, numbered
    trusted: bool        # the position is in the game's trusted moves
    start: float
    end: float
    text: str


def transcript(video_id: str) -> list[dict] | None:
    path = TRANSCRIPTS / f"{video_id}.json"
    return json.loads(path.read_text())["segments"] if path.exists() else None


def game_units(entry: dict, segs: list[dict]) -> list[Unit]:
    moves, times = entry["moves_san"], entry["move_seconds"]
    trusted = min(entry.get("trusted_ply", 0), len(moves))
    lo, hi = times[0] - LEAD, times[-1] + TAIL
    # bounds[p] = when position p (after p plies) appeared
    bounds = [lo] + list(times)
    by_ply: dict[int, list[dict]] = {}
    for s in segs:
        mid = (s["start"] + s["end"]) / 2
        if not lo <= mid < hi:
            continue
        p = max(i for i, b in enumerate(bounds) if b <= mid)
        by_ply.setdefault(p, []).append(s)
    out = []
    for p, ss in sorted(by_ply.items()):
        tail = max(0, p - 4)
        out.append(Unit(f"{entry['id']}:{p}", entry["id"], entry["video_id"], entry.get("channel") or "", p,
                        moves[p - 1] if p else "", positions.line_san(moves[tail:p], tail) if p else "",
                        p <= trusted, ss[0]["start"], ss[-1]["end"], " ".join(s["text"] for s in ss)))
    return out


def video_units(video_id: str, catalog: list[dict]) -> list[Unit] | None:
    segs = transcript(video_id)
    if segs is None:
        return None
    return [u for e in catalog if e["video_id"] == video_id and e["moves_san"] for u in game_units(e, segs)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video_id")
    ap.add_argument("--catalog", type=Path, default=positions.CATALOG)
    args = ap.parse_args(argv)
    us = video_units(args.video_id, json.loads(args.catalog.read_text()))
    if us is None:
        print("(no transcript)")
        return 1
    for u in us:
        print(json.dumps(asdict(u)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
