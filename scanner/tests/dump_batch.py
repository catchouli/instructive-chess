#!/usr/bin/env python3
"""Download and read (tests/dump_readings.py) many videos in parallel, so a
batch can be reconstructed later with `rescan.py --from-readings` - frame
reading is the slow part and doesn't change when reconstruction does.

    scanner/.venv/bin/python scanner/tests/dump_batch.py --jobs 6 --log DIR VIDEO_ID [...]

Videos whose readings already exist are skipped. A failed download is retried
once (YouTube sometimes answers 403 for a while).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / "scanner" / ".venv" / "bin" / "python"
DUMP = ROOT / "scanner" / "tests" / "dump_readings.py"
CACHE = ROOT / "scanner" / "cache"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video_ids", nargs="+")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--log", type=Path, required=True, help="directory for per-video logs")
    args = ap.parse_args(argv)
    args.log.mkdir(parents=True, exist_ok=True)

    def one(vid: str) -> tuple[str, str]:
        if (CACHE / f"{vid}-readings.json").exists():
            return vid, "cached"
        for attempt in range(2):
            with open(args.log / f"{vid}.log", "a") as log:
                code = subprocess.call([str(PYTHON), "-u", str(DUMP), f"https://www.youtube.com/watch?v={vid}"],
                                       stdout=log, stderr=subprocess.STDOUT)
            if code == 0:
                return vid, "ok"
        return vid, f"exit {code}"

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(one, v) for v in args.video_ids]
        for i, fut in enumerate(as_completed(futures), 1):
            vid, status = fut.result()
            print(f"[{i}/{len(args.video_ids)}] {vid}: {status}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
