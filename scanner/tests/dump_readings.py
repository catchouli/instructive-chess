#!/usr/bin/env python3
"""Run board detection + per-frame recognition once for a cached video and save the
raw (timestamp, placement) reading stream to disk, so reconstruction logic can be
iterated on instantly afterwards without re-running computer vision.

    scanner/.venv/bin/python scanner/tests/dump_readings.py <video-id-or-path> [--fps 1.0]

Writes scanner/cache/<video_id>-readings.json: {"box": [...], "fps": ..., "readings": [[ts, placement_or_null], ...]}
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import board_detect   # noqa: E402
import download       # noqa: E402
import recognize       # noqa: E402
from scan import iter_frames, sample_frames  # noqa: E402


def dump(target: str, fps: float = 1.0, start: float = 0.0, end: float = 0.0) -> Path:
    src = download.fetch(target)
    box = board_detect.detect(sample_frames(src.path, start, end, 15))
    if box is None:
        raise SystemExit("board not found")

    model = None
    readings = []
    for ts, frame in iter_frames(src.path, fps, start, end):
        if model is None:
            cand = None
            if recognize.looks_like_start(recognize.occupancy(frame, box)):
                cand, tuned = recognize.calibrate_from_start(frame, box)
            if cand is None:
                readings.append((ts, None))
                continue
            model, box = cand, tuned
            print(f"calibrated at t={int(ts)}s box={box.x},{box.y} {box.w}x{box.h}")
        rd = recognize.read_board(frame, box, model)
        if rd.confidence >= 0.72 and 4 <= rd.n_pieces <= 34:
            readings.append((ts, rd.placement))
        else:
            readings.append((ts, None))

    out = download.CACHE / f"{src.video_id}-readings.json"
    out.write_text(json.dumps({"box": box.as_tuple(), "fps": fps, "readings": readings}))
    print(f"wrote {out} ({len(readings)} readings, "
          f"{sum(1 for _, p in readings if p)} with a board)")
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("target")
    p.add_argument("--fps", type=float, default=1.0)
    p.add_argument("--start", type=float, default=0.0)
    p.add_argument("--end", type=float, default=0.0)
    args = p.parse_args()
    dump(args.target, args.fps, args.start, args.end)
