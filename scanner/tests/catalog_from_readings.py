#!/usr/bin/env python3
"""Reconstruct + catalog a video from an already-dumped readings cache, skipping
the (slow, CPU-heavy) board detection/recognition pass entirely.

Used to safely parallelize a batch re-scan: dump_readings.py can run for many
videos at once (no shared state), but writing to web/data/games.json is a
read-modify-write on one shared file, so that step must run one video at a
time - this script is the fast (~seconds) second half of that pipeline.

    scanner/.venv/bin/python scanner/tests/catalog_from_readings.py <video-id> <url> [--min-confidence 0.6]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import catalog        # noqa: E402
import download       # noqa: E402
import recognize       # noqa: E402
import reconstruct     # noqa: E402

CACHE = Path(__file__).resolve().parents[1] / "cache"


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("video_id")
    p.add_argument("url")
    p.add_argument("--min-confidence", type=float, default=0.6)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    readings_path = CACHE / f"{args.video_id}-readings.json"
    if not readings_path.exists():
        print(f"      no readings cache for {args.video_id} - run dump_readings.py first", file=sys.stderr)
        return 2
    readings = json.loads(readings_path.read_text())["readings"]

    src = download.fetch(args.url)
    voted = recognize.vote_stream(readings)
    games = reconstruct.reconstruct(voted, raw=dict(readings))
    kept = [g for g in games if g.confidence >= args.min_confidence and g.ply >= 2]
    print(f"      {src.title!r}: {len(games)} games found, {len(kept)} kept "
          f"(confidence >= {args.min_confidence})")
    for i, g in enumerate(kept):
        flip = " [flipped]" if g.flipped else ""
        print(f"        game {i + 1}: {g.ply} plies conf={g.confidence:.2f} t={int(g.start_seconds)}s{flip}")

    if args.dry_run:
        return 0

    added, total = catalog.merge(src, kept)
    print(f"      wrote {catalog.DATA_FILE.name}  (+{added} for this video, {total} total)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
