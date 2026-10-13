"""Rescan catalogued videos with the current scanner, in parallel, into a
separate catalog - so a full run can be scored (tests/benchmark.py --catalog)
before anything replaces web/data/games.json.

    scanner/.venv/bin/python scanner/tests/rescan.py --out /tmp/rescanned.json
    scanner/.venv/bin/python scanner/tests/rescan.py --out /tmp/x.json VIDEO_ID [VIDEO_ID ...]
    scanner/.venv/bin/python scanner/tests/rescan.py --out /tmp/x.json <youtube-url>   # a new video

Each video is scanned by its own scan.py process into its own scratch file
(scan.py's catalog write is a read-modify-write of one file, so parallel
scans can't share one), then everything is merged into --out. Videos not
rescanned keep their existing entries. Logs go next to --out.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import catalog  # noqa: E402
import download  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "web" / "data" / "games.json"
PYTHON = ROOT / "scanner" / ".venv" / "bin" / "python"
SCAN = ROOT / "scanner" / "scan.py"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video_ids", nargs="*", help="video IDs or URLs (new videos welcome); default: every video in the catalog")
    ap.add_argument("--out", type=Path, required=True, help="merged catalog to write")
    ap.add_argument("--jobs", type=int, default=6)
    args = ap.parse_args(argv)

    games = json.loads(CATALOG.read_text())
    urls: dict[str, str] = {}
    for g in games:
        urls.setdefault(g["video_id"], g["video_url"])
    # Videos not yet in the catalog (new additions) can be given as IDs or URLs.
    vids = []
    for v in args.video_ids or list(urls):
        vid = download.video_id_from_url(v) if download._looks_like_url(v) else v
        urls.setdefault(vid, v if download._looks_like_url(v) else f"https://www.youtube.com/watch?v={vid}")
        vids.append(vid)
    work = args.out.parent / (args.out.stem + "_parts")
    work.mkdir(parents=True, exist_ok=True)

    def scan_one(vid: str) -> tuple[str, int]:
        part = work / f"{vid}.json"
        part.write_text("[]\n")
        with open(work / f"{vid}.log", "w") as log:
            code = subprocess.call([str(PYTHON), str(SCAN), urls[vid], "--catalog", str(part)],
                                   stdout=log, stderr=subprocess.STDOUT)
        return vid, code

    failed = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(scan_one, v) for v in vids]
        for i, fut in enumerate(as_completed(futures), 1):
            vid, code = fut.result()
            if code != 0:
                failed.append(vid)
            print(f"[{i}/{len(vids)}] {vid}: {'ok' if code == 0 else f'exit {code}'}", flush=True)

    rescanned = set(vids) - set(failed)
    merged = [g for g in games if g["video_id"] not in rescanned]
    for vid in rescanned:
        merged += json.loads((work / f"{vid}.json").read_text())
    merged.sort(key=catalog._sort_key, reverse=True)   # same order scan.py writes
    args.out.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {args.out}: {len(merged)} games ({len(rescanned)} videos rescanned, "
          f"{len(failed)} failed{': ' + ' '.join(failed) if failed else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
