"""Regression check: rating localization + reading against real cached
videos, at real hi-res frames fetched fresh over the network (a rating
needs a higher resolution than the regular scan downloads - see
download.fetch_hires_frame). Skipped automatically if the video isn't
cached locally (videos aren't checked into git) or the network fetch fails
(e.g. offline) - this test needs both the cheap local low-res board-box
detection AND a fresh network fetch, unlike test_clock_read_ground_truth.py
which only needs the former.

Every spot check's expected value was read by eye from the actual video
frame (see conversation history for the zoomed screenshots used). All
current cases are exact - rating_read.py used to classify digits with a
hand-built template matcher that hit a real, well-tested accuracy ceiling
(see git history), replaced with Tesseract (pytesseract) for the actual
digit-reading step once it got every hard case gathered during that
investigation exactly right. Don't loosen an assertion to "close" without
a real reason (verify by eye first - a skip from a network hiccup is not
the same as a real miss), and don't add a new case without verifying its
expected value by eye against the real frame.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import board_detect     # noqa: E402
import download         # noqa: E402
import rating_read as rr  # noqa: E402
from scan import sample_frames  # noqa: E402

CACHE = Path(__file__).resolve().parents[1] / "cache"

# video_id -> (fetch timestamp, expected_top, expected_bottom)
CASES = {
    # "zaamot (1540)" / "SomeoneCallAnAmbulance (1528)" - both exact.
    "DktnyNKqdYU": (90, 1540, 1528),
    # "nasrhamzah (1291)" / "SomeoneCallAnAmbulance (1285)" - both exact.
    "H9EymHfVnSE": (27, 1291, 1285),
    # "Visvydas (2219)" / "SomeoneCallAnAmbulance (2395)" - both exact
    # (bottom was this module's worst case under the old template matcher,
    # never better than 2/4 digits right - now exact with Tesseract).
    "ao5Xxv7HqQ8": (78, 2219, 2395),
    # "IMRosen (2388)" / "IMRosen (2565)" - both exact. All 4 digits of the
    # top rating render merged into one touching blob at this resolution -
    # this was the single longest-standing unsolved case in this module's
    # history under the old template matcher (segmentation was correct,
    # per-digit classification wasn't); Tesseract reads a merged run
    # directly with no separate splitting step needed at all.
    "KfuyDm5uksI": (995, 2388, 2565),
}

# Cases where the reader doesn't yet get every digit right - checked with
# a looser "close" comparison (same digit count, at least half the digits
# in the right position) instead of exact equality, so a real regression
# (wrong digit *count*, or a wildly different value) still fails loudly
# without demanding perfection this module doesn't claim to have yet.
# Empty since switching to Tesseract - kept as infrastructure for whatever
# the next hard case turns out to be, not deleted.
LOOSE_TOLERANCE: dict[str, set[str]] = {}


def _digits_close(got: int | None, expected: int, min_matching_fraction: float = 0.5) -> bool:
    if got is None:
        return False
    g, e = str(got), str(expected)
    if len(g) != len(e):
        return False
    matches = sum(1 for a, b in zip(g, e) if a == b)
    return matches / len(e) >= min_matching_fraction


def _video_path(video_id: str) -> Path | None:
    matches = [p for p in CACHE.glob(f"{video_id}.*")
               if p.suffix in (".mkv", ".webm", ".mp4")]
    return matches[0] if matches else None


@pytest.mark.parametrize("video_id", list(CASES))
def test_rating_read_matches_ground_truth(video_id):
    ts, expected_top, expected_bottom = CASES[video_id]
    path = _video_path(video_id)
    if path is None:
        pytest.skip(f"no cached video for {video_id} - can't detect its board box")

    frames = sample_frames(path, 0, 0, 15)
    box = board_detect.detect(frames)
    if box is None:
        pytest.skip(f"board not found in cached low-res frames for {video_id}")

    info = download.CACHE / f"{video_id}.info.json"
    if not info.exists():
        pytest.skip(f"no .info.json for {video_id} - can't recover its real URL")
    import json
    url = json.loads(info.read_text()).get("webpage_url")
    if not url:
        pytest.skip(f"no webpage_url in {video_id}'s .info.json")

    frame = download.fetch_hires_frame(url, float(ts))
    if frame is None:
        pytest.skip(f"hi-res fetch failed for {video_id} (network/yt-dlp issue)")

    import cv2
    cap = cv2.VideoCapture(str(path))
    lowres_h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    cap.release()
    scale = frame.shape[0] / lowres_h
    scaled_box = (round(box.x * scale), round(box.y * scale),
                  round(box.w * scale), round(box.h * scale))

    reading = rr.read_ratings(frame, scaled_box)

    loose = LOOSE_TOLERANCE.get(video_id, set())
    if "top" in loose:
        assert _digits_close(reading.top, expected_top), \
            f"{video_id} top: got {reading.top}, expected close to {expected_top}"
    else:
        assert reading.top == expected_top, \
            f"{video_id} top: got {reading.top}, expected exactly {expected_top}"
    if "bottom" in loose:
        assert _digits_close(reading.bottom, expected_bottom), \
            f"{video_id} bottom: got {reading.bottom}, expected close to {expected_bottom}"
    else:
        assert reading.bottom == expected_bottom, \
            f"{video_id} bottom: got {reading.bottom}, expected exactly {expected_bottom}"
