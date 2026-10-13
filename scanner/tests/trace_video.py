#!/usr/bin/env python3
"""Explain how reconstruction treated a video: replay its cached readings
(tests/dump_readings.py) through reconstruct.reconstruct with a trace, and
show each game against the real game it matches (tests/benchmark.py's
truth sources) - where it opened, where it diverged, why it closed.

    scanner/.venv/bin/python scanner/tests/trace_video.py VIDEO_ID [--from SECONDS --to SECONDS] [--all]

Without --all, only the trace around each game's first wrong move and its
end is printed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import benchmark      # noqa: E402
import catalog        # noqa: E402
import download       # noqa: E402
import recognize      # noqa: E402
import reconstruct    # noqa: E402

CACHE = Path(__file__).resolve().parents[1] / "cache"


def _show_readings(voted, g, real, p: int) -> None:
    import chess
    board = chess.Board()
    for san in real["moves_san"][:p]:
        board.push_san(san)
    truths = []
    b = board.copy()
    for san in real["moves_san"][p:p + 3]:
        b.push_san(san)
        truths.append(reconstruct._placement_of(b))
    before = reconstruct._placement_of(board)
    bw = board.copy()
    bw.push_san(g.moves_san[p])
    wrong = reconstruct._placement_of(bw)
    t_prev = g.move_seconds[p - 1] if p else g.start_seconds
    t_wrong = g.move_seconds[p]
    print(f"   readings {t_prev - 2:.0f}-{t_wrong + 10:.0f}s, squares off: before | real +1 +2 +3 | wrong "
          f"({' '.join(real['moves_san'][p:p + 3])} vs {g.moves_san[p]})")
    for ts, pl in voted:
        if not (t_prev - 2 <= ts <= t_wrong + 10):
            continue
        if pl is None:
            print(f"     {ts:8.1f}  -")
            continue
        tgt = pl[::-1] if g.flipped else pl
        hs = [reconstruct._hamming(tgt, t) for t in truths]
        print(f"     {ts:8.1f}  {reconstruct._hamming(tgt, before):2d} | "
              f"{' '.join(f'{h:2d}' for h in hs)} | {reconstruct._hamming(tgt, wrong):2d}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video_id")
    ap.add_argument("--from", dest="t0", type=float, default=0.0)
    ap.add_argument("--to", dest="t1", type=float, default=0.0)
    ap.add_argument("--all", action="store_true", help="print the whole trace")
    ap.add_argument("--context", type=int, default=6, help="trace lines shown around each point of interest")
    ap.add_argument("--readings", action="store_true",
                    help="also print the voted readings around each first wrong ply, as squares off "
                         "the real positions (after n, n+1, n+2 plies) and off the recorded wrong one")
    args = ap.parse_args(argv)

    rp = CACHE / f"{args.video_id}-readings.json"
    if not rp.exists():
        print(f"no {rp.name} - run tests/dump_readings.py first", file=sys.stderr)
        return 2
    readings = json.loads(rp.read_text())["readings"]
    if args.t1:
        readings = [(ts, p) for ts, p in readings if args.t0 <= ts <= args.t1]
    elif args.t0:
        readings = [(ts, p) for ts, p in readings if ts >= args.t0]
    media = next((m for m in CACHE.glob(f"{args.video_id}.*") if m.suffix in (".mkv", ".webm", ".mp4")), None)
    src = download.fetch(str(media)) if media else None

    trace: list[tuple[float, str]] = []
    voted = recognize.vote_stream(readings)
    games = reconstruct.reconstruct(voted, trace=lambda ts, m: trace.append((ts, m)))
    print(f"{len(games)} games reconstructed, {len(trace)} trace lines")

    cands = []
    if src is not None:
        try:
            cands = benchmark.candidates_for_video(src.channel, src.published, src.video_id)
        except Exception as e:  # network / no accounts
            print(f"(no truth: {e})")

    if args.all:
        for ts, m in trace:
            print(f"  {ts:8.1f}  {m}")

    for i, g in enumerate(games):
        entry = {"moves_san": g.moves_san, "ambiguous_plies": list(g.ambiguous)}
        real, why = benchmark.match(entry, cands) if cands else (None, "no truth")
        line = f"\ngame {i + 1}: t={int(g.start_seconds)}s {g.ply} plies conf={g.confidence:.2f} skipped={g.skipped}"
        if real is None:
            print(f"{line}  [unmatched: {why}]")
            print("   " + g.pgn()[:200])
            points = [g.move_seconds[-1]] if g.move_seconds else []
        else:
            p = benchmark._prefix(g.moves_san, real["moves_san"], g.ambiguous)
            print(f"{line}  real {len(real['moves_san'])} plies ({real['white']} v {real['black']}), "
                  f"{'all right' if p == g.ply else f'first wrong ply {p + 1}: got {g.moves_san[p]} real {real['moves_san'][p] if p < len(real['moves_san']) else '-'}'}")
            points = []
            if p < g.ply:
                points.append(g.move_seconds[p])
                if args.readings:
                    _show_readings(voted, g, real, p)
            if g.move_seconds:
                points.append(g.move_seconds[-1])
        if args.all:
            continue
        shown: set[int] = set()
        for t in points:
            idx = max(range(len(trace)), key=lambda k: (trace[k][0] <= t, trace[k][0]))
            for k in range(max(0, idx - args.context), min(len(trace), idx + args.context + 1)):
                if k not in shown:
                    shown.add(k)
                    print(f"  {trace[k][0]:8.1f}  {trace[k][1]}")
            print("  ...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
