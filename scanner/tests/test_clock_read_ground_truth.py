"""Regression check: clock localization + reading against real cached videos.

Unlike test_ground_truth.py (which replays cached board *readings*, no source
video needed), this needs actual video frames - clock pixels aren't part of
the board-reading cache - so it decodes a real ~2-minute window straight from
the cached source video via scan.iter_frames. Skipped automatically if that
video isn't cached locally (videos aren't checked into git).

Coverage (what fraction of frames get a reading at all) is a much weaker
signal than it looks: this module would rather report nothing than a wrong
value, so a low fraction can mean "working correctly, just conservative" as
easily as "broken". The spot checks below - a handful of frames whose
on-screen clock value was read by hand from the actual video - are what
actually catch a correctness regression; the coverage floor just catches a
localization/wiring break (finding zero candidates, crashing, etc).

To add a case for a new video: find any ~2-minute span with an active,
visible clock (a known game's start_seconds from games.json works well),
note a handful of (timestamp, on-screen value) pairs by eye, and add it to
CASES below.
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

# video_id -> (window start second, minimum fraction of frames read at all,
#              [(timestamp, expected_seconds), ...] hand-verified spot checks)
#
# Coverage here is deliberately a low bar (see module docstring): both videos
# used to read a much larger fraction of frames before this module's voting
# redesign, but silently wrong a meaningful chunk of the time (confirmed by
# hand - e.g. KfuyDm5uksI's "3:00" opening was previously read as "6:10" for
# several frames in a row, self-consistently, with no way to tell from the
# output alone). A later fix (requiring a segment to confirm itself with at
# least 2 independent wrap-anchors before it's allowed to contribute to the
# shared digit templates) recovered most of that coverage back honestly -
# KfuyDm5uksI went from 20/120 to 101/120 correct frames from that change
# alone, including a second, previously-unverified game later in the same
# window that spot-check (1093, 119.0) below now confirms reads correctly.
# A second fix (rejecting wrap-anchor candidates whose "before" glyph
# doesn't match the shape most other anchors in the same segment settle on,
# and counting real wraps between anchors from tens-digit change events
# instead of rounding the raw ones-tick gap) fixed 9h8UcYJZz-Q's
# 45-59-second window, previously silently wrong by up to 20 seconds after
# a dropped-frames hiccup in the source stream skipped the on-screen clock
# ahead by 5 seconds in a single sampled frame - spot-checks (61, 590.0)
# through (89, 560.0) below span that fix.
#
# Don't raise these floors without re-verifying spot checks still pass by
# hand against the actual video; a real improvement should let you *add*
# more spot checks; a bigger fraction on its own proves nothing.
CASES = {
    "KfuyDm5uksI": (995, 0.75, [
        (995, 180.0),   # "3:00"
        (1004, 179.0),  # "2:59"
        (1014, 169.0),  # "2:49"
        (1093, 119.0),  # "1:59" - a second, later game in the same window
    ]),
    "9h8UcYJZz-Q": (45, 0.95, [
        (50, 600.0),    # "10:00" - frozen, game hasn't started ticking yet
        (61, 590.0),    # "09:50"
        (79, 570.0),    # "09:30" - right after a dropped-frames hiccup in the
                         # source stream skips the on-screen clock ahead by 5
                         # seconds in a single sampled frame (t=70 -> t=71,
                         # "09:41" -> "09:36"); confirms the skip no longer
                         # throws off anchors past it (see _wrap_anchors and
                         # _corrected_ticks)
        (80, 569.0),    # "09:29"
        (89, 560.0),    # "09:20"
        (162, 494.0),   # "08:14"
        (163, 493.0),   # "08:13"
    ]),
}


def _video_path(video_id: str) -> Path | None:
    matches = [p for p in CACHE.glob(f"{video_id}.*") if p.suffix in (".mkv", ".webm", ".mp4")]
    return matches[0] if matches else None


@pytest.mark.skip(reason="exercises the legacy self-calibrating clock_read.read_clock, which scan.py no "
                         "longer uses (replaced by read_clock_ocr - see test_clock_read_ocr_ground_truth.py, "
                         "which covers the same windows); kept for the history in its comments")
@pytest.mark.parametrize("video_id", list(CASES))
def test_clock_localized_and_correct(video_id):
    start, min_frac, spot_checks = CASES[video_id]
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

    best_frac, best_readings = 0.0, None
    for box in candidates:
        readings = clock_read.read_clock(frames, times, box)
        frac = sum(1 for r in readings if r.seconds is not None) / len(readings)
        if frac > best_frac:
            best_frac, best_readings = frac, readings

    assert best_frac >= min_frac, (
        f"best clock candidate for {video_id} only read {best_frac:.0%} of frames, "
        f"expected at least {min_frac:.0%}")

    by_t = {r.t: r.seconds for r in best_readings}
    for t, expected in spot_checks:
        actual = by_t.get(float(t))
        assert actual == expected, (
            f"{video_id} at t={t}s: expected {expected}s on-screen, got {actual}")
