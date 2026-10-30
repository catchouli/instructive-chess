"""Read again the stretches of a video where the board couldn't be read:
with the board's new box where it moved mid-stream, and with templates
fitted for the stretch itself where it didn't (a board theme or piece set
changed between games: YzMxojtbDME's green board read 7 pieces at 0.69
confidence with the video's templates, 31 at 0.87 with ones fitted there).

The 1 fps readings are taken with one board box and one set of piece
templates per video. A stretch where under POOR_SHARE of POOR_FRAMES readings
in a row read a board is read again at 1 fps - with the box found again in
it, or the one in use if that didn't move - and with the best templates
fitted on its first opening frames (best_fit), and kept where it reads more
boards than before. The readings file gets a "segments" list - from which
time on which box applies, each stretch with its own templates - that scan.py
follows for the 6 fps frames, clocks and ratings. The old file is kept as
<id>-readings.pre-relocate.json; a video done by this pass (marked in
<id>-relocate.json) is left alone. Pass 3 (extend) moves the boundaries between stretches,
with the templates a rescan cached for each (so it runs after one); the readings pass 2 left
are kept as <id>-readings.pre-extend.json.

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

POOR_FRAMES = 60      # readings in a row...
POOR_SHARE = 0.5      # ...with under this share readable: a stretch to read again
SAME_PX = 8           # a box found again within this many pixels (and 2% size) is the same box
PASS = 3              # 1: moved boards only (LOST 120 readings under 10%); 2: poor stretches; 3: + extend()


def poor_stretches(readings: list[tuple[float, str | None]]) -> list[tuple[float, float]]:
    """(start, end) of each stretch covered by POOR_FRAMES readings in a row with under
    POOR_SHARE readable, after the first readable minute (end: the next reading after it)."""
    first = next((t for t, p in readings if p), None)
    if first is None:
        return []
    ts = [t for t, _ in readings]
    ok = [p is not None for _, p in readings]
    poor = [False] * len(ok)
    run = sum(ok[:POOR_FRAMES])
    for i in range(POOR_FRAMES - 1, len(ok)):
        if i >= POOR_FRAMES:
            run += ok[i] - ok[i - POOR_FRAMES]
        if ts[i - POOR_FRAMES + 1] >= first + 60 and run < POOR_SHARE * POOR_FRAMES:
            poor[i - POOR_FRAMES + 1:i + 1] = [True] * POOR_FRAMES
    out, i = [], 0
    while i < len(poor):
        if poor[i]:
            j = i
            while j + 1 < len(poor) and poor[j + 1]:
                j += 1
            out.append((ts[i], ts[j + 1] if j + 1 < len(ts) else ts[j] + 1.0))
            i = j + 1
        else:
            i += 1
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


def box_at(data: dict, segments: list[dict], t: float) -> list:
    """The box in use at time t."""
    return next((g["box"] for g in reversed(segments) if g["t"] <= t), data["box"])


STEP = 30         # extend() moves a boundary this many seconds at a time...
STEP_SAMPLES = 5  # ...where the other templates read better on most of this many frames in the step
MORE_PIECES = 4   # better: a confident read where the other isn't, or this many more pieces


def _confident(rd) -> bool:
    return rd.confidence >= 0.72 and 4 <= rd.n_pieces <= 34


def reads_better(new, old) -> bool:
    """One frame read with two sets of templates: does `new` read it clearly better?"""
    return _confident(new) and (not _confident(old) or new.n_pieces >= old.n_pieces + MORE_PIECES)


def extend(vid: str, media: Path, data: dict, readings: list, segments: list[dict]) -> tuple[list, list[dict], list[str]]:
    """Move each boundary between two stretches (scan.py's parts, with the templates the rescan
    cached for them) back or forward, a STEP at a time, while the templates on the far side read
    the frames clearly better than the ones in use. A stretch is cut where under half its frames
    read a board, but a board theme changed earlier still reads, with pieces missing
    (fRhVZueNenU: from 590 s the video's templates read 3-8 pieces, those fitted from 873 s 20-29,
    the game that started there was never formed). A boundary never reaches the next one, so
    each part keeps its index and its cached templates. Returns (readings, segments, notes)."""
    parts = [(0.0, data["box"], "")] + [(g["t"], g["box"], f"-{i}") for i, g in enumerate(segments, 1)]
    models = [scan.load_model(vid, box, suffix=suf) for _, box, suf in parts]
    if any(m is None for m, _ in models):
        return readings, segments, ["extend: templates not cached (rescan first)"]
    end = readings[-1][0] + 1.0
    notes = []
    for j in range(len(parts) - 1):
        b = segments[j]["t"]
        lo = (segments[j - 1]["t"] if j else 0.0) + STEP
        hi = (segments[j + 1]["t"] if j + 1 < len(segments) else end) - STEP
        for side, (win, lose) in (("back", (j + 1, j)), ("forward", (j, j + 1))):
            edge = b
            while (edge - STEP >= lo) if side == "back" else (edge + STEP <= hi):
                a0 = edge - STEP if side == "back" else edge
                frames = list(scan.iter_frames(media, STEP_SAMPLES / STEP, a0, a0 + STEP))[:STEP_SAMPLES]
                wins = sum(reads_better(recognize.read_board(f, models[win][1], models[win][0]),
                                        recognize.read_board(f, models[lose][1], models[lose][0])) for _, f in frames)
                if not frames or wins * 2 <= len(frames):
                    break
                edge = a0 if side == "back" else a0 + STEP
            if edge == b:
                continue
            a, z = sorted((edge, b))
            _, _, more = scan.read_frames(scan.iter_frames(media, 1.0, a, z), models[win][1], model=models[win][0])
            more = [(round(ts, 3), p) for ts, p in more if a <= ts < z]
            gained = sum(p is not None for _, p in more) - sum(p is not None for ts, p in readings if a <= ts < z)
            readings = sorted([r for r in readings if not a <= r[0] < z] + more, key=lambda r: r[0])
            segments[j] = {**segments[j], "t": edge}
            notes.append(f"extend {b:.0f}s {side} to {edge:.0f}s ({gained:+d} boards read)")
            b = edge
            if side == "back":
                break   # the boundary moved back: the old one is inside the stretch now
    return readings, segments, notes


def relocate(vid: str, dry_run: bool = False) -> str:
    cache = download.CACHE
    path = cache / f"{vid}-readings.json"
    mark = cache / f"{vid}-relocate.json"
    try:
        data = json.loads(path.read_text())
        media = next(m for m in cache.glob(f"{vid}.*") if m.suffix in (".mkv", ".webm", ".mp4"))
    except (FileNotFoundError, StopIteration):
        return f"{vid}: no readings or video"
    done = json.loads(mark.read_text()).get("pass", 0) if mark.exists() else 0
    if done >= PASS:
        return f"{vid}: done"
    recognize.TUNE_GRID = data.get("tune_grid", 0.0)
    readings = [(t, p) for t, p in data["readings"]]
    segments: list[dict] = [g for i, g in enumerate(data.get("segments", []))
                            if not any(h["t"] == g["t"] for h in data["segments"][i + 1:])]
    notes, changed = [], False
    for a, stop in (poor_stretches(readings) if done < 2 else []):
        cur = box_at(data, segments, a)
        found = board_detect.detect(scan.sample_frames(media, a + min(60.0, (stop - a) / 4), min(stop, a + 1260), 15))
        box = found if found is not None and not same_box(found.as_tuple(), cur) else board_detect.Box(*cur, score=1.0)
        how = "same box" if box.as_tuple() == tuple(cur) else f"box {list(cur)} -> {list(box.as_tuple())}"
        model, tuned = best_fit(media, box, a, stop)
        if model is None:
            notes.append(f"{a:.0f}-{stop:.0f}s {how}: no opening position")
            continue
        model, box_read, more = scan.read_frames(scan.iter_frames(media, 1.0, a, stop), tuned, model=model)
        more = [(round(ts, 3), p) for ts, p in more if ts < stop]
        gained = sum(p is not None for _, p in more) - sum(p is not None for ts, p in readings if a <= ts < stop)
        if gained <= 0:
            notes.append(f"{a:.0f}-{stop:.0f}s {how}: reads {-gained} fewer boards")
            continue
        after = box_at(data, segments, stop)
        readings = sorted([r for r in readings if not a <= r[0] < stop] + more, key=lambda r: r[0])
        segments = [g for g in segments if not a <= g["t"] < stop]
        segments.append({"t": a, "box": [int(v) for v in box_read.as_tuple()]})
        if stop < readings[-1][0] and not any(g["t"] == stop for g in segments):
            segments.append({"t": stop, "box": list(after)})   # what was in use after the stretch
        segments.sort(key=lambda g: g["t"])
        notes.append(f"{a:.0f}-{stop:.0f}s {how}: {gained} more boards read")
        changed = True
    if done == 2 and segments:
        readings, segments, more_notes = extend(vid, media, data, readings, segments)
        notes += more_notes
        changed |= any(n.startswith("extend ") for n in more_notes)
    if not dry_run:
        if changed:
            backup = cache / f"{vid}-readings.pre-relocate.json"
            if not backup.exists():
                shutil.copy2(path, backup)
            if done == 2 and not (cache / f"{vid}-readings.pre-extend.json").exists():
                shutil.copy2(path, cache / f"{vid}-readings.pre-extend.json")   # as pass 2 left it
            scan.write_json(path, {**data, "readings": readings, "segments": segments})
        scan.write_json(mark, {"pass": PASS, "notes": notes})
    return f"{vid}: " + ("; ".join(notes) if notes else "nothing poor")


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
