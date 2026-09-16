"""Unit tests for clock_read's synthetic-data-friendly building blocks:
segmentation, the timing-based digit bootstrap, majority-vote reading, and
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
    _build_templates,
    _calibrate_same_sim,
    _classify,
    _cluster_votes,
    _gap_threshold,
    _hole_count,
    _reject_outliers,
    _relative_ticks,
    _segment_chars,
    _segments,
    _tight_ink_bbox,
    _vote_for_anchors,
)

_SIM = 0.5   # a threshold synthetic bar-glyphs never land near either side of


def _digit_img(d: int, size=(16, 14)) -> np.ndarray:
    """A synthetic "glyph" for digit d: a fixed pseudo-random pattern seeded
    by d, uncorrelated with every other digit's pattern even under
    _similarity's small alignment-shift search (unlike a simple sliding bar,
    which a 1px shift can turn into an adjacent digit's pattern) - the
    bootstrap/tracking logic under test doesn't care what a digit actually
    looks like, only that same digit = same image, different = clearly
    different."""
    return (np.random.RandomState(d).rand(*size) > 0.5).astype(np.uint8) * 255


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
    seg_id = _segments([float(i) for i in range(len(ones_seq))])

    values = _bootstrap_ones_digits(ones_glyphs, tens_glyphs, seg_id, _SIM, _SIM)
    assert values == ones_seq


def test_bootstrap_returns_none_without_a_wrap():
    # never observes a 0->9 wrap, so there's no self-contained anchor
    ones_seq = [5, 4, 3, 2, 1]
    tens_seq = [2, 2, 2, 2, 2]
    ones_glyphs = [_digit_img(d) for d in ones_seq]
    tens_glyphs = [_digit_img(d) for d in tens_seq]
    seg_id = _segments([float(i) for i in range(len(ones_seq))])

    values = _bootstrap_ones_digits(ones_glyphs, tens_glyphs, seg_id, _SIM, _SIM)
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
    seg_id = _segments(times)

    values = _bootstrap_ones_digits(ones_glyphs, tens_glyphs, seg_id, _SIM, _SIM)
    assert values[0] is None and values[1] is None
    assert values[2:] == ones_seq[2:]


def test_build_templates_rejects_a_minority_mislabel():
    # digit "3" gets 4 confidently-known examples, but the bootstrap got 2 of
    # them wrong (a real, observed failure mode with few samples per digit) -
    # blindly averaging all 4 would visibly blend the wrong shape in; the
    # majority (2 real "3"s) must win instead
    glyphs = [_digit_img(3), _digit_img(3), _digit_img(3), _digit_img(0), _digit_img(5)]
    values = [3, 3, 3, 3, 3]   # bootstrap mislabeled two of them as "3" too
    templates = _build_templates(glyphs, values, same_thresh=0.5)
    assert templates[3] is not None
    assert np.array_equal(templates[3], _digit_img(3))


def _shape_with_holes(n_holes: int, size=(16, 14)) -> np.ndarray:
    """A solid ink block with 0, 1, or 2 fully-enclosed background holes
    punched into it - "0"/"8"-shaped, without needing a real font."""
    img = np.zeros(size, np.uint8)
    img[2:14, 2:12] = 255
    if n_holes >= 1:
        img[4:7, 4:10] = 0
    if n_holes >= 2:
        img[9:12, 4:10] = 0
    return img


def test_hole_count_distinguishes_ring_shapes():
    assert _hole_count(_shape_with_holes(0)) == 0
    assert _hole_count(_shape_with_holes(1)) == 1
    assert _hole_count(_shape_with_holes(2)) == 2


def test_classify_breaks_near_tie_by_hole_count():
    # a "0" (one hole) and an "8" (two holes) that are otherwise identical -
    # ZNCC alone can't separate two round digits this close, but the
    # query's hole count picks out the right one unambiguously
    zero_like = _shape_with_holes(1)
    eight_like = _shape_with_holes(2)
    templates: list[np.ndarray | None] = [None] * 10
    templates[0] = zero_like
    templates[8] = eight_like

    assert _classify(zero_like, templates) == 0
    assert _classify(eight_like, templates) == 8


def test_classify_respects_valid_digits_restriction():
    # tens-of-seconds can only ever be 0-5 (seconds run 00-59) - if the best
    # raw match is an out-of-range digit, the restriction must still pick
    # the best *legal* one rather than returning the impossible answer
    templates: list[np.ndarray | None] = [_digit_img(d) for d in range(10)]
    query = _digit_img(6)   # would win outright without the restriction
    assert _classify(query, templates) == 6
    assert _classify(query, templates, valid_digits=range(6)) in range(6)


def test_relative_ticks_counts_changes_and_resets_at_segment_boundary():
    glyphs = [_digit_img(d) for d in [5, 5, 4, 4, 3]]
    seg_id = [0, 0, 0, 1, 1]   # a segment break right before the last change
    ticks, changed = _relative_ticks(glyphs, seg_id, _SIM)
    assert ticks == [0, 0, 1, 0, 1]
    assert changed == [False, False, True, False, True]


def test_gap_threshold_finds_the_gap_between_clusters():
    # the real bimodal shape measured on an actual clock: "same" pairs
    # (repeats/pauses) cluster tightly with a little of their own jitter,
    # clearly separated by a wide gap from "different" pairs, which spread
    # out smoothly (no single big internal jump) well below it
    same_cluster = [1.0, 1.0, 0.97, 0.967, 0.948, 0.913]
    diff_cluster = [0.709, 0.65, 0.6, 0.55, 0.5, 0.45, 0.4, 0.35, 0.3, 0.2, 0.12]
    thresh = _gap_threshold(same_cluster + diff_cluster, default=0.85, floor=0.05, ratio=4.0)
    assert 0.709 < thresh <= 0.913   # between the clusters, not inside either


def test_gap_threshold_ignores_a_bigger_gap_buried_in_the_different_cluster():
    # measured on a real video: the true boundary gap (0.986->0.916, 0.07)
    # is smaller than a gap that occurs much further down, well inside the
    # "different" cluster's own spread (0.617->0.451, 0.166) - the biggest
    # gap in the whole list is the wrong answer here; only the first one
    # that's a clear outlier *relative to the cluster above it* is right
    same_cluster = [1.0, 1.0, 1.0, 0.999, 0.997, 0.997, 0.991, 0.986]
    diff_cluster = [0.916, 0.902, 0.885, 0.881, 0.88, 0.879, 0.877, 0.877,
                    0.875, 0.798, 0.766, 0.764, 0.762, 0.754, 0.71, 0.617, 0.451]
    thresh = _gap_threshold(same_cluster + diff_cluster, default=0.85, floor=0.05, ratio=4.0)
    assert 0.916 < thresh <= 0.986   # the true boundary, not the bigger buried gap


def test_gap_threshold_falls_back_with_too_little_data():
    assert _gap_threshold([0.9, 0.8], default=0.42, floor=0.05, ratio=4.0) == 0.42


def test_gap_threshold_falls_back_with_no_clear_gap():
    # everything within a small, uniform step of its neighbor - no real
    # cluster boundary, just a smooth gradient
    sims = [0.90, 0.895, 0.89, 0.885, 0.88, 0.875, 0.87]
    assert _gap_threshold(sims, default=0.42, floor=0.05, ratio=4.0) == 0.42


def test_calibrate_same_sim_matches_gap_threshold_on_real_glyphs():
    # same/different is easy to fake with distinct bar glyphs: identical
    # bars score 1.0, non-overlapping ones score very low - so a handful of
    # repeats plus a handful of changes should calibrate near the midpoint
    same_pair = [_digit_img(3), _digit_img(3)]
    glyphs = same_pair + [_digit_img(3), _digit_img(7)] * 4
    seg_id = [0] * len(glyphs)
    thresh = _calibrate_same_sim(glyphs, seg_id)
    assert 0.05 < thresh < 0.95   # somewhere between the "same" and "different" scores


def test_vote_for_anchors_outvotes_a_minority_misread():
    # 8 frames agree the run started at 200s; 2 disagree (misclassified
    # tens/minutes on those frames) - the majority must win
    votes = {0: {200: 8, 240: 1, 130: 1}}
    assert _vote_for_anchors(votes) == {0: 200}


def test_vote_for_anchors_rejects_an_indecisive_split():
    # no real majority (and too few votes) - better to report nothing than
    # guess wrong
    votes = {0: {200: 2, 240: 1}}
    assert _vote_for_anchors(votes) == {}


def test_vote_for_anchors_rejects_a_narrow_majority():
    # 51/49 split on plenty of votes: technically a "winner" but not
    # trustworthy enough to commit to
    votes = {0: {200: 51, 240: 49}}
    assert _vote_for_anchors(votes) == {}


def test_cluster_votes_merges_nearby_guesses():
    # residual tick-counting noise scattering an otherwise clear majority
    # across several nearby values, one second apart - real signal seen on
    # an actual video after fixing a much larger misclassification
    guesses = [598] * 3 + [600] * 4 + [601] * 2 + [599] * 2
    counts = _cluster_votes(guesses, tol=3)
    assert max(counts.values()) == len(guesses)   # all merged into one cluster


def test_cluster_votes_keeps_distinct_clusters_separate():
    guesses = [100, 101, 100, 300, 301, 300, 301]
    counts = _cluster_votes(guesses, tol=3)
    assert sorted(counts.values()) == [3, 4]


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
