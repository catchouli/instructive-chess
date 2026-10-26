#!/usr/bin/env python3
"""Merge the games of a scratch catalog (tests/rescan.py --out) into another
catalog, replacing every video the scratch catalog covers:

    scanner/.venv/bin/python scanner/tests/merge_catalog.py SCRATCH.json [--into web/data/games.json] [--only VIDEO_ID ...]

Tags (pov, speedrun) and the rating sanity check are re-derived for the
merged videos (see catalog.tag / catalog.reconcile_ratings).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import catalog  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scratch", type=Path)
    ap.add_argument("--into", type=Path, default=catalog.DATA_FILE)
    ap.add_argument("--only", nargs="*", default=None, help="merge only these videos")
    args = ap.parse_args(argv)

    fresh = json.loads(args.scratch.read_text())
    if args.only:
        fresh = [g for g in fresh if g["video_id"] in set(args.only)]
    videos = {g["video_id"] for g in fresh}
    by_video: dict[str, list[dict]] = {}
    for g in fresh:
        catalog.tag(g)
        by_video.setdefault(g["video_id"], []).append(g)
    blanked = sum(catalog.reconcile_ratings(v) for v in by_video.values())

    existing = [g for g in catalog.load(args.into) if g["video_id"] not in videos]
    merged = existing + fresh
    ids = [g["id"] for g in merged]
    if len(set(ids)) != len(ids):
        # a scratch catalog that lists a video twice (e.g. two runs pasted together) would list each game twice
        dup = sorted({i for i in ids if ids.count(i) > 1})
        sys.exit(f"refusing to merge: {len(dup)} game ids appear twice ({' '.join(dup[:5])} ...)")
    merged.sort(key=catalog._sort_key, reverse=True)
    catalog.write(merged, args.into)
    print(f"merged {len(fresh)} games from {len(videos)} videos into {args.into} "
          f"({len(merged)} games total, {blanked} implausible ratings blanked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
