"""Read again, with the board's new box, the stretches of a video where the
board moved mid-stream and nothing was read after.

The 1 fps readings are taken with one board box per video. When the stream
layout changes (the board moves or is resized), every frame after reads as no
board: across the library, 241 such stretches held 29 h of video. For each
stretch of LOST_FRAMES readings in a row with under LOST_SHARE readable, the
board is detected again in the frames after it; if the box changed, the
stretch - up to where the old box reads again - is read at 1 fps with the new
box (templates calibrated at its first opening position, as a first read
does) and kept where it reads more boards than before. The readings file then gets a "segments" list - from which
time on which box applies - that scan.py follows for the 6 fps frames, clocks
and ratings. The old file is kept as <id>-readings.pre-relocate.json; a video
already relocated is left alone.

    relocate.py [--jobs N] [--dry-run] VIDEO_ID [...]     (default: every video in the catalog)
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scanner"))
import board_detect  # noqa: E402
import download      # noqa: E402
import recognize     # noqa: E402
import scan          # noqa: E402

LOST_FRAMES = 120     # readings in a row...
LOST_SHARE = 0.1      # ...with under this share readable: the board is lost
SAME_PX = 8           # a box found again within this many pixels (and 2% size) is the same box


def lost_stretches(readings: list[tuple[float, str | None]]) -> list[tuple[float, float | None]]:
    """(start, end) of each stretch where the board is lost - LOST_FRAMES readings in a
    row with under LOST_SHARE readable, after the first readable minute - until the
    next readable reading (None: never again)."""
    out: list[tuple[float, float | None]] = []
    first = next((t for t, p in readings if p), None)
    if first is None:
        return out
    win: list[bool] = []
    resume = first + 60
    for i, (ts, p) in enumerate(readings):
        if ts < resume:
            continue
        win.append(p is not None)
        if len(win) > LOST_FRAMES:
            win.pop(0)
        if len(win) == LOST_FRAMES and sum(win) < LOST_SHARE * LOST_FRAMES:
            back = next((t for t, q in readings[i + 1:] if q), None)
            out.append((ts - LOST_FRAMES + 1, back))
            win = []
            resume = back if back is not None else math.inf
    return out


FIT_TRIES = 5     # opening frames fitted in a stretch...
FIT_CHECK = 40    # ...each scored on this many frames sampled from it


def best_fit(media: Path, box, a: float, b: float | None):
    """Templates for a stretch, as scan.fit_model_to_readings fits them for a rescan: fitted
    on each of its first FIT_TRIES opening frames (the first is often mid-way through the
    layout change: fitted there, 2 of 825 frames read) and kept by how many of FIT_CHECK
    sampled frames they read with confidence. (model, box) or (None, None)."""
    span = (b or 0.0) - a if b else None
    every = max(1, int((span or 3600) / FIT_CHECK))
    fits, check = [], []
    for i, (ts, frame) in enumerate(scan.iter_frames(media, 1.0, a, b or 0.0)):
        if i % every == 0:
            check.append(frame)
        if len(fits) < FIT_TRIES and recognize.looks_like_start(recognize.occupancy(frame, box)):
            model, tuned = recognize.calibrate_from_start(frame, box)
            if model is not None:
                fits.append((model, tuned))
    if not fits:
        return None, None

    def score(fit):
        model, tuned = fit
        return sum(rd.confidence >= 0.72 and 4 <= rd.n_pieces <= 34
                   for rd in (recognize.read_board(f, tuned, model) for f in check))
    return max(fits, key=score)


def same_box(a, b) -> bool:
    return abs(a[2] - b[2]) <= 0.02 * b[2] and abs(a[0] - b[0]) <= SAME_PX and abs(a[1] - b[1]) <= SAME_PX


def relocate(vid: str, dry_run: bool = False) -> str:
    cache = download.CACHE
    path = cache / f"{vid}-readings.json"
    try:
        data = json.loads(path.read_text())
        media = next(m for m in cache.glob(f"{vid}.*") if m.suffix in (".mkv", ".webm", ".mp4"))
    except (FileNotFoundError, StopIteration):
        return f"{vid}: no readings or video"
    if data.get("segments"):
        return f"{vid}: already relocated"
    recognize.TUNE_GRID = data.get("tune_grid", 0.0)
    readings = [(t, p) for t, p in data["readings"]]
    end = readings[-1][0]
    segments: list[dict] = []
    notes = []
    for a, b in lost_stretches(readings):
        stop = b if b is not None else end
        box = board_detect.detect(scan.sample_frames(media, a + min(60.0, (stop - a) / 4), min(stop, a + 1260), 15))
        if box is None or same_box(box.as_tuple(), data["box"]):
            notes.append(f"lost {a:.0f}-{stop:.0f}s, {'no board' if box is None else 'same box'}")
            continue
        model, tuned = best_fit(media, box, a, b)
        if model is None:
            notes.append(f"lost {a:.0f}-{stop:.0f}s, box {list(box.as_tuple())}: no opening position")
            continue
        model, box_read, more = scan.read_frames(scan.iter_frames(media, 1.0, a, b or 0.0), tuned, model=model)
        more = [(round(ts, 3), p) for ts, p in more if ts < stop]
        gained = sum(p is not None for _, p in more) - sum(p is not None for ts, p in readings if a <= ts < stop)
        if model is None or gained <= 0:
            notes.append(f"lost {a:.0f}-{stop:.0f}s, box {list(box.as_tuple())}: "
                         f"{'no opening position' if model is None else f'reads {-gained} fewer boards'}")
            continue
        readings = [r for r in readings if not a <= r[0] < stop] + more
        readings.sort(key=lambda r: r[0])
        segments = [g for g in segments if g["t"] != a]   # a stretch starting where the last ended replaces its return
        segments.append({"t": a, "box": [int(v) for v in box_read.as_tuple()]})
        if b is not None:
            segments.append({"t": b, "box": list(data["box"])})   # the board is back where it was
        notes.append(f"lost {a:.0f}-{stop:.0f}s, box {list(data['box'])} -> {segments[-1 if b is None else -2]['box']}: "
                     f"{gained} more boards read")
    if segments and not dry_run:
        backup = cache / f"{vid}-readings.pre-relocate.json"
        if not backup.exists():
            shutil.copy2(path, backup)
        scan.write_json(path, {**data, "readings": readings, "segments": segments})
    return f"{vid}: " + ("; ".join(notes) if notes else "never lost")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("videos", nargs="*")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    args = ap.parse_args()
    vids = args.videos or sorted({g["video_id"] for g in json.loads((ROOT / "web/data/games.json").read_text())})
    with ProcessPoolExecutor(args.jobs) as ex:
        for i, line in enumerate(ex.map(relocate, vids, [args.dry_run] * len(vids)), 1):
            print(f"[{i}/{len(vids)}] {line}", flush=True)


if __name__ == "__main__":
    main()
