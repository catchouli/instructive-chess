"""Regression check: clock_read.read_clock_ocr (the Tesseract-based clock
reader that replaced self-calibration in scan.py - see its docstring)
against real cached videos. Mirrors test_clock_read_ground_truth.py's
pattern (skipped automatically if the video isn't cached locally).

Two of the cases below are not just spot checks - they're specifically the
real videos where the OLD self-calibrating read_clock was caught reporting
a *confidently wrong* value for an entire game (not just a coverage gap):
one game self-calibration read as "11:54" throughout was actually "10:00"
the whole time (654s off), another it read as "5:00" was actually "10:00" -
both confirmed by eye against the real frame. Keep these two cases even if
the older test file's own numbers are never touched; they're the direct
evidence for the switch and losing them would remove the reason this
module made the change from the "old vs. new" test suite entirely.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import board_detect  # noqa: E402
import clock_read    # noqa: E402
import scan          # noqa: E402

CACHE = Path(__file__).resolve().parents[1] / "cache"

# video_id -> (window_start, window_end, [(t, expected_seconds), ...])
CASES = {
    "KfuyDm5uksI": (995, 1120, [
        (995, 180.0), (1004, 179.0), (1014, 169.0), (1093, 119.0),
    ]),
    "9h8UcYJZz-Q": (45, 165, [
        (50, 600.0), (61, 590.0), (79, 570.0), (80, 569.0),
        (89, 560.0), (162, 494.0), (163, 493.0),
    ]),
    # Confirmed by eye: true value is "10:00" throughout this whole window -
    # self-calibration read this same clock as "11:54" the entire game.
    "DktnyNKqdYU": (41, 51, [
        (41, 600.0), (42, 599.0), (43, 598.0),
    ]),
    # Confirmed by eye: true value is "10:00" - self-calibration read this
    # same clock as "9:00" (540s, later drifting further) throughout.
    "ACDV-fVH_mo": (1258, 1268, [
        (1258, 600.0), (1259, 600.0), (1260, 599.0),
    ]),
}


def _video_path(video_id: str) -> Path | None:
    matches = [p for p in CACHE.glob(f"{video_id}.*") if p.suffix in (".mkv", ".webm", ".mp4")]
    return matches[0] if matches else None


@pytest.mark.parametrize("video_id", list(CASES))
def test_clock_ocr_matches_ground_truth(video_id):
    start, end, spot_checks = CASES[video_id]
    path = _video_path(video_id)
    if path is None:
        pytest.skip(f"no cached video for {video_id} - can't decode clock frames without it")

    frames = [f for _, f in scan.iter_frames(path, 1.0, start, end)]
    times = [start + i for i in range(len(frames))]
    assert len(frames) >= 8, "expected a real window of frames"

    board_box = board_detect.detect(frames[:30] if len(frames) >= 30 else frames)
    assert board_box is not None, "board must be detectable to exclude it from clock search"

    candidates = clock_read.find_clock_regions(frames, board_box)
    assert candidates, f"no clock region found for {video_id}"

    # Try every candidate, keep whichever one matches the most spot checks -
    # mirrors how scan.py picks a side per candidate box, without needing
    # this test to hardcode which of possibly-several candidates is "the"
    # one for a given video.
    best_matches, best_readings = -1, None
    for box in candidates:
        readings = clock_read.read_clock_ocr(frames, times, box)
        by_t = {r.t: r.seconds for r in readings}
        matches = sum(1 for t, expected in spot_checks if by_t.get(float(t)) == expected)
        if matches > best_matches:
            best_matches, best_readings = matches, readings

    by_t = {r.t: r.seconds for r in best_readings}
    for t, expected in spot_checks:
        actual = by_t.get(float(t))
        assert actual == expected, (
            f"{video_id} at t={t}s: expected {expected}s on-screen, got {actual}")
