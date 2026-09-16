"""Regression check: clock localization + reading against real cached videos.

Unlike test_ground_truth.py (which replays cached board *readings*, no source
video needed), this needs actual video frames - clock pixels aren't part of
the board-reading cache - so it decodes a real ~2-minute window straight from
the cached source video via scan.iter_frames. Skipped automatically if that
video isn't cached locally (videos aren't checked into git).

To add a case for a new video: find any ~2-minute span with an active,
visible clock (a known game's start_seconds from games.json works well) and
add it to CASES below.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import board_detect  # noqa: E402
import clock_read    # noqa: E402
import scan           # noqa: E402

CACHE = Path(__file__).resolve().parents[1] / "cache"

# video_id -> (window start second, minimum fraction of frames read successfully)
#
# Digit reading is timing-bootstrapped from the clock's own countdown (see
# clock_read.py's module docstring) and works well most of the time, but
# still occasionally misreads a position for a few frames before the next
# real change self-corrects it - these thresholds reflect that current,
# still-improving baseline. Don't raise them without re-measuring; a real
# accuracy improvement should be validated by hand against a few frames'
# actual on-screen values, not just by clearing a higher bar.
CASES = {
    "KfuyDm5uksI": (995, 0.75),
    "9h8UcYJZz-Q": (45, 0.75),
}


def _video_path(video_id: str) -> Path | None:
    matches = [p for p in CACHE.glob(f"{video_id}.*") if p.suffix in (".mkv", ".webm", ".mp4")]
    return matches[0] if matches else None


@pytest.mark.parametrize("video_id", list(CASES))
def test_clock_localized_and_mostly_readable(video_id):
    start, min_frac = CASES[video_id]
    path = _video_path(video_id)
    if path is None:
        pytest.skip(f"no cached video for {video_id} - can't decode clock frames without it")

    frames = [f for _, f in scan.iter_frames(path, 1.0, start, start + 120)]
    times = [start + i for i in range(len(frames))]
    assert len(frames) >= 60, "expected ~120s of frames from a real game window"

    board_box = board_detect.detect(frames[:30])
    assert board_box is not None, "board must be detectable to exclude it from clock search"

    candidates = clock_read.find_clock_regions(frames, board_box)
    assert candidates, f"no clock region found for {video_id}"

    best_frac = 0.0
    for box in candidates:
        readings = clock_read.read_clock(frames, times, box)
        frac = sum(1 for r in readings if r.seconds is not None) / len(readings)
        best_frac = max(best_frac, frac)

    assert best_frac >= min_frac, (
        f"best clock candidate for {video_id} only read {best_frac:.0%} of frames, "
        f"expected at least {min_frac:.0%}")
