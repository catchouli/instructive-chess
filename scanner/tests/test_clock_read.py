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
    ClockBox,
    ClockReading,
    assign_clock_sides,
    live_move_count,
    clock_at_moves,
    _parse_clock_text,
    _physically_consistent,
    _bootstrap_ones_digits,
    _build_templates,
    _calibrate_same_sim,
    _classify,
    _cluster_votes,
    _corrected_ticks,
    _gap_threshold,
    _hole_count,
    _is_colon,
    _is_decimal_point,
    _reject_outliers,
    _relative_ticks,
    _segment_chars,
    _segments,
    _strip_decimal_tenths,
    _strip_leading_icon,
    _tight_ink_bbox,
    _vote_for_anchors,
    _wrap_anchors,
    clock_at_times,
    find_clock_regions,
    side_for_box,
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


def test_is_colon_distinguishes_sparse_dots_from_a_digit():
    # a colon: two small ink bands with a real gap - at least one fully-empty
    # row between its topmost and bottommost ink
    mask = np.zeros((16, 6), np.uint8)
    mask[4:6, 2:4] = 1
    mask[10:12, 2:4] = 1
    assert _is_colon(mask, (0, 5, 0, 15)) is True

    # a digit: one continuous vertical stroke, even a hollow one like "0" -
    # every row from top to bottom ink has *some* ink, so no internal gap
    digit = np.zeros((16, 6), np.uint8)
    digit[1:15, 0:6] = 1
    digit[5:11, 2:4] = 0   # hollow center - still leaves ink in every row
    assert _is_colon(digit, (0, 5, 0, 15)) is False


def test_is_decimal_point_distinguishes_a_low_dot_from_a_colon_or_digit():
    # a decimal point: a single small dot near the bottom of the row, far
    # short of spanning the full digit height - confirmed on a real video,
    # chess.com switches to "M:SS.T" once a clock drops under ~4 seconds
    row_h = 16
    point = np.zeros((row_h, 6), np.uint8)
    point[12:15, 2:4] = 1   # small, near the bottom
    assert _is_decimal_point(point, (0, 5, 0, row_h - 1)) is True

    # a colon spans most of the row height (two dots near top and bottom)
    colon = np.zeros((row_h, 6), np.uint8)
    colon[3:5, 2:4] = 1
    colon[11:13, 2:4] = 1
    assert _is_decimal_point(colon, (0, 5, 0, row_h - 1)) is False

    # a full digit obviously isn't a decimal point either
    digit = np.zeros((row_h, 6), np.uint8)
    digit[1:15, 0:6] = 1
    assert _is_decimal_point(digit, (0, 5, 0, row_h - 1)) is False


def test_strip_decimal_tenths_drops_a_trailing_period_and_digit():
    # "M:SS.T" (a clock in tenths-of-a-second mode) should be treated as
    # plain "M:SS" - the tenths digit and its point are noise this module
    # never needed at whole-second precision anyway
    row_h = 16
    mask = np.zeros((row_h, 40), np.uint8)
    chars = [
        (0, 4, 0, row_h - 1),    # minutes digit
        (6, 7, 0, row_h - 1),    # colon (content doesn't matter for this test)
        (9, 13, 0, row_h - 1),   # tens-of-seconds digit
        (15, 19, 0, row_h - 1),  # ones-of-seconds digit
        (21, 23, 0, row_h - 1),  # decimal point (small, near bottom)
        (25, 29, 0, row_h - 1),  # tenths digit
    ]
    mask[1:15, 0:4] = 1
    mask[3:5, 6:7] = 1
    mask[11:13, 6:7] = 1
    mask[1:15, 9:13] = 1
    mask[1:15, 15:19] = 1
    mask[12:15, 21:23] = 1   # low, short blob - the decimal point
    mask[1:15, 25:29] = 1

    stripped = _strip_decimal_tenths(mask, chars)
    assert stripped == chars[:4]

    # a plain "M:SS" with no trailing tenths is left alone
    assert _strip_decimal_tenths(mask, chars[:4]) == chars[:4]


def test_strip_leading_icon_drops_a_far_off_leading_blob():
    # a ticking-clock icon sitting well to the left of the real "M:SS" text
    # (confirmed on a real video, chess.com): a ~38-60px gap before the real
    # text vs. 2-3px between the real characters themselves
    chars = [
        (0, 14, 0, 29),    # icon (its own width varies a lot in practice -
                           # this check works off the gap, not the width)
        (53, 61, 0, 29),   # minutes digit
        (64, 66, 0, 29),   # colon
        (69, 78, 0, 29),   # tens-of-seconds
        (81, 90, 0, 29),   # ones-of-seconds
    ]
    assert _strip_leading_icon(chars) == chars[1:]


def test_strip_leading_icon_keeps_a_real_leading_minutes_digit():
    # ordinary "M:SS" spacing - every gap is roughly the same size, so
    # nothing should be dropped even if the leading digit happens to be
    # narrow (e.g. a "1")
    chars = [
        (0, 3, 0, 29),     # a narrow "1" as the minutes digit
        (6, 8, 0, 29),     # colon
        (11, 20, 0, 29),   # tens-of-seconds
        (23, 32, 0, 29),   # ones-of-seconds
    ]
    assert _strip_leading_icon(chars) == chars


def test_strip_leading_icon_leaves_short_lists_alone():
    # fewer than 3 characters means there's no second gap to compare
    # against - nothing to safely judge "anomalously large" relative to
    chars = [(0, 14, 0, 29), (53, 61, 0, 29)]
    assert _strip_leading_icon(chars) == chars


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


def test_wrap_anchors_rejects_a_skip_induced_false_wrap():
    # 3 genuine wraps (ones and tens change together, "before" glyph always
    # a real "0") plus one fake: a dropped-frames-style skip that also
    # flips both digits in one step, but whose "before" glyph is "5", not
    # "0" - a real video hit this exact case ("09:41" -> "09:36" in one
    # sampled frame). With enough genuine wraps to form a majority, the
    # fake one's mismatched shape should get it rejected.
    ones_seq = (
        [3, 2, 1, 0, 9] +                          # wrap A: idx3(0) -> idx4(9)
        [8, 7, 6, 5, 4, 3, 2, 1, 0, 9] +           # wrap B: idx13(0) -> idx14(9)
        [8, 7, 6, 5, 0] +                          # fake jump: idx18(5) -> idx19(0)
        [9, 8, 7, 6, 5, 4, 3, 2, 1, 0, 9]          # wrap C: idx29(0) -> idx30(9)
    )
    tens_seq = (
        [5] * 4 + [4] * 10 + [3] * 5 + [2] * 11 + [1]
    )
    assert len(ones_seq) == len(tens_seq) == 31
    ones_glyphs = [_digit_img(d) for d in ones_seq]
    tens_glyphs = [_digit_img(d) for d in tens_seq]
    seg_id = [0] * len(ones_seq)

    anchors = _wrap_anchors(ones_glyphs, tens_glyphs, seg_id, _SIM, _SIM)
    idxs = [idx for _, idx in anchors[0]]
    assert 18 not in idxs             # the fake, mismatched-shape anchor is rejected
    assert set(idxs) == {3, 13, 29}   # only the genuine "0"-shaped wraps remain


def test_corrected_ticks_counts_real_wraps_from_tens_changes():
    # raw ones-ticks alone say the gap between these two anchors is 14 -
    # close enough to round to 10 (one wrap) - but a dropped-frames skip
    # undercounted it: 2 real wraps actually happened, confirmed here by 2
    # tens-digit change events in between. Counting those directly (rather
    # than rounding the raw gap) must recover the true 20-tick gap.
    seg_id = [0] * 21
    wrap_anchors = {0: [(0, 0), (14, 20)]}
    tens_changed = [False] * 21
    tens_changed[5] = True
    tens_changed[15] = True
    rel_ticks = [0] * 21
    rel_ticks[10] = 7    # halfway through the raw tick gap
    rel_ticks[20] = 14

    offsets = _corrected_ticks(rel_ticks, seg_id, wrap_anchors, tens_changed)
    assert offsets[0] == 0
    assert offsets[20] == 20   # 2 real wraps, not round(14 / 10) * 10 == 10
    assert offsets[10] == 10   # halfway through raw ticks -> halfway through corrected span


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


def _inset(x0, x1, y0, y1, m=4):
    return x0 + m, x1 - m, y0 + m, y1 - m


def test_find_clock_regions_recovers_a_rarely_changing_minutes_digit():
    # A "M:SS"-shaped label where the minutes digit changes only once across
    # the whole sequence (far below _CHANGE_LO) while the seconds pair right
    # next to it changes on ~half the frames - realistic for a long time
    # control sampled over a whole game: a minutes digit that changes once
    # every ~60 real ticks never clears the frequency threshold on its own,
    # so the raw frequency-based blob only ever covers the seconds pair.
    # Confirmed on a real video: a 149px-wide "09:41" label localized as
    # just a 54px "41" blob over a ~7-minute game. find_clock_regions must
    # still recover the *full* label by widening into the real (unchanging)
    # pixel content, not just the changing blob.
    h, w = 60, 120
    min_box = _inset(20, 39, 15, 44)
    # two small dots with a real gap between them, not a solid block - a
    # solid rectangle doesn't read as a colon to _is_colon (see below), and
    # find_clock_regions now specifically requires a real colon-shaped
    # character to accept a candidate (see its own docstring)
    colon_dot1 = (44, 47, 25, 28)
    colon_dot2 = (44, 47, 32, 35)
    tens_box = _inset(52, 71, 15, 44)
    ones_box = _inset(76, 95, 15, 44)

    frames = []
    val = 50
    for i in range(30):
        if i % 2 == 0 and i > 0:
            val = 200 if val == 50 else 50
        img = np.zeros((h, w), np.uint8)
        img[min_box[2]:min_box[3], min_box[0]:min_box[1]] = 100 if i == 15 else 200
        img[colon_dot1[2]:colon_dot1[3], colon_dot1[0]:colon_dot1[1]] = 180
        img[colon_dot2[2]:colon_dot2[3], colon_dot2[0]:colon_dot2[1]] = 180
        img[tens_box[2]:tens_box[3], tens_box[0]:tens_box[1]] = val
        img[ones_box[2]:ones_box[3], ones_box[0]:ones_box[1]] = val
        frames.append(np.stack([img] * 3, axis=-1))

    board_box = (0, 0, 1, 1)   # nowhere near the synthetic clock - a no-op exclusion
    cands = find_clock_regions(frames, board_box)
    assert len(cands) == 1
    box = cands[0]
    # the recovered box must span from the (rarely-changing) minutes digit
    # through to the ones-of-seconds digit, not just the fast-changing pair
    assert box.x <= min_box[0]
    assert box.x + box.w >= ones_box[1]


def test_side_for_box_uses_board_position_and_flip():
    board_box = (100, 200, 400, 400)   # x, y, w, h - vertical center at y=400
    top_box = ClockBox(x=500, y=100, w=50, h=30)
    bottom_box = ClockBox(x=500, y=650, w=50, h=30)

    # standard orientation: bottom of screen/board is White
    assert side_for_box(bottom_box, board_box, flipped=False) == "white"
    assert side_for_box(top_box, board_box, flipped=False) == "black"
    # flipped: Black is shown at the bottom instead
    assert side_for_box(bottom_box, board_box, flipped=True) == "black"
    assert side_for_box(top_box, board_box, flipped=True) == "white"


def test_clock_at_times_takes_the_last_known_value_at_or_before_each_query():
    readings = [
        ClockReading(10.0, 300.0),
        ClockReading(11.0, None),      # a dropped/unreadable frame in between
        ClockReading(12.0, 298.0),
        ClockReading(15.0, 295.0),
    ]
    # queries land exactly on a reading, between readings, and before the first
    assert clock_at_times(readings, [10.0, 11.5, 12.0, 13.5, 15.0]) == \
        [300.0, 300.0, 298.0, 298.0, 295.0]
    assert clock_at_times(readings, [5.0]) == [None]   # before any reading at all


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


def _readings(n_ok: int, n_none: int) -> list[ClockReading]:
    return ([ClockReading(t=float(i), seconds=300.0 - i) for i in range(n_ok)]
            + [ClockReading(t=float(n_ok + i), seconds=None) for i in range(n_none)])


def test_assign_clock_sides_prefers_candidates_that_read_as_clocks():
    # Real case: moving T-shirt text outranked the real top clock by area;
    # it never OCRs as a time, so it must lose to the clock that does.
    board = (600, 60, 560, 560)
    shirt = ClockBox(240, 670, 120, 47)
    bottom = ClockBox(1150, 675, 96, 32)
    top = ClockBox(1150, 13, 93, 33)
    cands = [(shirt, _readings(2, 98)), (bottom, _readings(95, 5)), (top, _readings(90, 10))]
    sides = assign_clock_sides(cands, board, flipped=False, min_coverage=0.5)
    assert sides["white"] is cands[1][1]
    assert sides["black"] is cands[2][1]


def test_assign_clock_sides_uses_relative_position_for_a_stacked_panel():
    # Real case: both clocks stacked in a panel above the board's middle -
    # the upper one is still the top player's.
    board = (560, 0, 717, 717)
    upper = ClockBox(444, 70, 48, 27)
    lower = ClockBox(441, 114, 53, 28)
    cands = [(lower, _readings(80, 0)), (upper, _readings(80, 0))]
    normal = assign_clock_sides(cands, board, flipped=False, min_coverage=0.5)
    assert normal["black"] is cands[1][1] and normal["white"] is cands[0][1]
    flipped = assign_clock_sides(cands, board, flipped=True, min_coverage=0.5)
    assert flipped["white"] is cands[1][1] and flipped["black"] is cands[0][1]


def test_assign_clock_sides_ignores_the_same_clock_found_twice():
    board = (300, 4, 654, 654)
    wide = ClockBox(864, 438, 238, 46)
    narrow = ClockBox(922, 438, 180, 43)
    cands = [(wide, _readings(90, 10)), (narrow, _readings(95, 5))]
    sides = assign_clock_sides(cands, board, flipped=True, min_coverage=0.5)
    assert list(sides.values()) == [cands[1][1]]


def test_parse_clock_text_ignores_trailing_border_junk():
    # Real case: the clock widget's border OCR'd as a trailing "|".
    assert _parse_clock_text("3:58|") == 238
    assert _parse_clock_text("2:4]") is None   # still conservative on a truncated time


def test_parse_clock_text_no_bare_digit_guess_after_trimming_junk():
    # Real case: "2:59" OCR'd as "F200}" - trimmed to "200" it would read as
    # a confident but wrong 2:00.
    assert _parse_clock_text("F200}") is None
    assert _parse_clock_text("0930") == 570   # a clean colon-less read still works


def _ticking(side_times: list[tuple[float, float]]) -> list[ClockReading]:
    return [ClockReading(t=t, seconds=v) for t, v in side_times]


def test_live_move_count_drops_moves_after_both_clocks_stop():
    # Real shape: after the last real move (t=204) black thinks and resigns;
    # then 4 "moves" appear with both clocks frozen.
    moves = [100, 110, 120, 130, 140, 150, 170, 190, 204, 208, 208, 215, 215]
    white = _ticking([(t, 300 - min(t, 150) + 100) for t in range(100, 230)])
    black = _ticking([(t, 400 - min(t, 206) + 100) for t in range(100, 230)])
    assert live_move_count(moves, {"white": white, "black": black}) == 9


def test_live_move_count_keeps_a_mating_final_move_and_needs_both_clocks():
    moves = [100, 150, 204, 208]
    frozen = _ticking([(t, 300.0 if t > 150 else 300.0 - (t - 100)) for t in range(100, 215)])
    also = _ticking([(t, 200.0 if t > 204 else 250.0 - (t - 150) * 0.5) for t in range(100, 215)])
    sides = {"white": frozen, "black": also}
    assert live_move_count(moves, sides, game_over_after=lambda n: n == 4) == 4
    assert live_move_count(moves, {"white": frozen}) == 4


def test_parse_clock_text_takes_the_time_at_the_end():
    # Real case: a board coordinate "6" beside the clock read as a leading digit.
    assert _parse_clock_text("~—606:55") == 415
    assert _parse_clock_text("12:34") == 754
    assert _parse_clock_text("1:22:46") == 4966


def test_clock_at_moves_uses_the_value_held_while_frozen():
    # White moves at t=10 (seen ~2s early: clock still running until 12),
    # black replies at t=20 but is seen late (t=21, white's clock already
    # running again). The frozen value in between is what counts.
    white_clock = [ClockReading(t=float(t), seconds=v) for t, v in
                   [(8, 300), (9, 299), (10, 298), (11, 297), (12, 296), (13, 296), (14, 296),
                    (15, 296), (16, 296), (17, 296), (18, 296), (19, 296), (20, 296), (21, 295)]]
    times = [10.0, 21.0]
    assert clock_at_moves(white_clock, times, [True, False]) == [296, 295]


def test_physically_consistent_drops_a_repeated_misread():
    # Real shape: a dimmed idle clock misread the same way for 5 frames
    # ("8:50" -> 49), then read right again.
    vals = [532, 531, 530, 49, 49, 49, 49, 49, 529, 528, 527]
    rd = [ClockReading(t=float(1024 + i), seconds=v) for i, v in enumerate(vals)]
    out = [r.seconds for r in _physically_consistent(rd)]
    assert out == [532, 531, 530, None, None, None, None, None, 529, 528, 527]


def test_physically_consistent_keeps_a_frozen_clock_and_an_increment():
    vals = [300, 299, 298, 298, 298, 298, 303, 302]   # frozen, then +5 increment
    rd = [ClockReading(t=float(i), seconds=v) for i, v in enumerate(vals)]
    assert [r.seconds for r in _physically_consistent(rd)] == vals
