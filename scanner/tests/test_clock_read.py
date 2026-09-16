"""Unit tests for clock_read's synthetic-data-friendly building blocks:
segmentation, the timing-based digit bootstrap, change-aware tracking, and
outlier rejection. See test_clock_read_ground_truth.py for end-to-end checks
against real video.

The two segmentation bugs below were found on real clock widgets: a widget's
own border ran edge-to-edge across the crop and survived the fixed pixel
margin, so every column looked non-empty and the whole label collapsed into
one "character"; and a small/compressed clock rendered its digits close
enough together that adjacent glyphs were only a single pixel apart, which
the old gap threshold didn't count as a break.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clock_read import (  # noqa: E402
    ClockReading,
    _bootstrap_ones_digits,
    _reject_outliers,
    _segment_chars,
    _tight_ink_bbox,
    _track_slot,
)


def _digit_img(d: int, size=(16, 14)) -> np.ndarray:
    """A synthetic "glyph" for digit d: a vertical bar at a digit-specific
    column, distinct enough for ZNCC to tell any two apart but cheap to
    construct - the bootstrap/tracking logic under test doesn't care what
    a digit actually looks like, only that same digit = same image."""
    img = np.zeros(size, np.uint8)
    img[:, d:d + 2] = 255
    return img


def _widget(h=45, w=124, border=3, bg=151):
    """A gray crop shaped like a real clock widget: a solid border frame
    running the full width/height, a quiet background inside it, and some
    "digit ink" placed away from the border."""
    img = np.full((h, w), bg, np.uint8)
    img[:border, :] = 40
    img[-border:, :] = 40
    img[:, :border] = 40
    img[:, -border:] = 40
    return img


def test_tight_ink_bbox_ignores_full_span_border():
    img = _widget()
    # a digit-shaped blob well inside the border, not touching any edge
    img[15:30, 60:70] = 255
    img[15:30, 80:90] = 255

    res = _tight_ink_bbox(img, margin=4, thresh=40)
    assert res is not None
    mask, (x0, x1, y0, y1) = res
    # the border must not have leaked into the tight bbox
    assert x0 >= 50 and x1 <= 95
    assert y0 >= 5 and y1 <= 35


def test_tight_ink_bbox_returns_none_when_only_border_present():
    img = _widget()  # no digit ink at all, just the frame
    res = _tight_ink_bbox(img, margin=4, thresh=40)
    assert res is None


def test_segment_chars_splits_on_single_pixel_gap():
    # two 4px-wide glyphs separated by exactly one empty column, as a small
    # or heavily-compressed clock renders them
    mask = np.zeros((10, 9), bool)
    mask[2:8, 0:4] = True
    mask[2:8, 5:9] = True
    chars = _segment_chars(mask, (0, 8, 0, 9))
    assert len(chars) == 2


def test_segment_chars_counts_four_for_mm_ss():
    # "2:21"-shaped ink: digit, thin colon, digit, digit, each separated by
    # at least one empty column
    mask = np.zeros((10, 20), bool)
    mask[2:8, 0:4] = True    # "2"
    mask[4:6, 6:8] = True    # ":"
    mask[2:8, 10:14] = True  # "2"
    mask[2:8, 16:19] = True  # "1"
    chars = _segment_chars(mask, (0, 19, 0, 9))
    assert len(chars) == 4


def test_bootstrap_anchors_ones_digit_from_tens_wrap():
    # a real countdown: ones cycles 9->0 every 10 ticks, tens only decrements
    # the instant ones wraps 0->9 - two wraps here, at index 4 and index 14
    ones_seq = [3, 2, 1, 0, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0, 9]
    tens_seq = [5, 5, 5, 5, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 3]
    ones_glyphs = [_digit_img(d) for d in ones_seq]
    tens_glyphs = [_digit_img(d) for d in tens_seq]
    times = [float(i) for i in range(len(ones_seq))]

    values = _bootstrap_ones_digits(ones_glyphs, tens_glyphs, times)
    assert values == ones_seq


def test_bootstrap_returns_none_without_a_wrap():
    # never observes a 0->9 wrap, so there's no self-contained anchor
    ones_seq = [5, 4, 3, 2, 1]
    tens_seq = [2, 2, 2, 2, 2]
    ones_glyphs = [_digit_img(d) for d in ones_seq]
    tens_glyphs = [_digit_img(d) for d in tens_seq]
    times = [float(i) for i in range(len(ones_seq))]

    values = _bootstrap_ones_digits(ones_glyphs, tens_glyphs, times)
    assert all(v is None for v in values)


def test_bootstrap_breaks_across_an_unreliable_gap():
    # a dropped-frame gap much larger than the sampling cadence, with no wrap
    # before it (segment 1) and a wrap fully after it (segment 2). Ticks
    # can't be assumed to advance by exactly one per glyph change across the
    # gap itself, so segment 1 must stay unanchored while segment 2 - which
    # does see a complete wrap - calibrates fine on its own.
    ones_seq = [2, 1, 5, 4, 3, 2, 1, 0, 9]
    tens_seq = [3, 3, 2, 2, 2, 2, 2, 2, 1]   # tens decrements exactly at the wrap
    times = [0.0, 1.0, 100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0]
    ones_glyphs = [_digit_img(d) for d in ones_seq]
    tens_glyphs = [_digit_img(d) for d in tens_seq]

    values = _bootstrap_ones_digits(ones_glyphs, tens_glyphs, times)
    assert values[0] is None and values[1] is None
    assert values[2:] == ones_seq[2:]


def test_track_slot_carries_value_through_unchanged_frames():
    templates = [None] * 10
    templates[7] = _digit_img(7)
    templates[3] = _digit_img(3)
    glyphs = [_digit_img(7), _digit_img(7), _digit_img(7), _digit_img(3)]
    seg_id = [0, 0, 0, 0]
    values = _track_slot(glyphs, seg_id, templates)
    assert values == [7, 7, 7, 3]


def test_track_slot_handles_absent_slot():
    # a tens-of-minutes digit that doesn't exist yet in the first two frames
    templates = [None] * 10
    templates[1] = _digit_img(1)
    glyphs = [None, None, _digit_img(1), _digit_img(1)]
    seg_id = [0, 0, 0, 0]
    values = _track_slot(glyphs, seg_id, templates)
    assert values == [None, None, 1, 1]


def test_reject_outliers_drops_single_frame_spike():
    readings = [
        ClockReading(0.0, 100.0),
        ClockReading(1.0, 99.0),
        ClockReading(2.0, 250.0),   # matches neither neighbor's trend
        ClockReading(3.0, 97.0),
        ClockReading(4.0, 96.0),
    ]
    out = _reject_outliers(readings)
    assert [r.seconds for r in out] == [100.0, 99.0, None, 97.0, 96.0]


def test_reject_outliers_keeps_a_real_increment():
    # a move-increment bump: doesn't match the *previous* reading's trend,
    # but the sequence continues decreasing from it, matching the *next*
    readings = [
        ClockReading(0.0, 100.0),
        ClockReading(1.0, 99.0),
        ClockReading(2.0, 101.0),   # +2s increment, then resumes counting down
        ClockReading(3.0, 100.0),
        ClockReading(4.0, 99.0),
    ]
    out = _reject_outliers(readings)
    assert [r.seconds for r in out] == [100.0, 99.0, 101.0, 100.0, 99.0]
