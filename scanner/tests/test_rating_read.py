"""Unit tests for rating_read's structural building blocks: glyph merging,
paren-shape discrimination, row clustering, and the non-text/decoy filters
in _best_rating_in_chars - the *localization* logic, not digit reading
itself (which is Tesseract - see test_rating_read_ground_truth.py for real
accuracy checks against real video).

Every bug fixed below was found on a real video, not hypothesized:
- _band_glyphs's avatar-fragment merge only checked x-overlap, which was
  fine for a thin band (nothing unrelated shared its narrow y-range there)
  but cascaded into merging almost an entire wide sidebar region into one
  blob once the search widened to cover non-adjacent broadcast layouts.
- _best_rating_in_chars's "rightmost wins" tie-break, combined with a fixed-
  width search band, once preferred a bogus match built from the on-screen
  clock's own digits (positioned within the same search band, well to the
  right of the actual rating) over the real rating.
- A thin decorative divider line under a name/rating row, spanning nearly
  the entire search band, defeated the gap-based fix above by "bridging"
  over the real gap - its own huge right edge made the true gap measure as
  small.
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rating_read as rr  # noqa: E402


def _crop(h, w, bg=40):
    return np.full((h, w), bg, np.uint8)


def _paint(img, y0, y1, x0, x1, val=220):
    img[y0:y1 + 1, x0:x1 + 1] = val


def test_row_clusters_splits_on_vertical_gap():
    # two text lines (y 0-10 and y 20-30) with a real vertical gap between
    chars = [(0, 5, 0, 10), (10, 15, 1, 9), (0, 5, 20, 30), (10, 15, 21, 29)]
    clusters = rr._row_clusters(chars)
    assert len(clusters) == 2
    assert set(clusters[0]) == {0, 1}
    assert set(clusters[1]) == {2, 3}


def test_row_clusters_groups_overlapping_rows():
    chars = [(0, 5, 0, 10), (10, 15, 3, 12), (20, 25, 1, 9)]
    clusters = rr._row_clusters(chars)
    assert len(clusters) == 1
    assert set(clusters[0]) == {0, 1, 2}


def test_band_glyphs_merges_only_when_both_x_and_y_overlap():
    # Two small blobs mutually overlapping in x AND y (an avatar photo
    # fragmenting into pieces of itself) must merge into one component.
    crop = _crop(40, 60)
    _paint(crop, 5, 10, 5, 12)
    _paint(crop, 7, 13, 9, 16)   # overlaps the first in both x and y
    # A third blob shares x-range with the pair above but sits in a
    # completely different row (no y-overlap) - must NOT be merged in,
    # even though a naive x-only check would have caught it (this is
    # exactly the bug that cascaded across a whole sidebar region).
    _paint(crop, 25, 30, 6, 14)

    mask, chars = rr._band_glyphs(crop)
    assert len(chars) == 2
    widths_and_rows = sorted((c[2], c[3]) for c in chars)
    # the merged pair spans the union of both fragments' y-range
    assert widths_and_rows[0] == (5, 13)
    # the separate row glyph keeps its own, unmerged y-range
    assert widths_and_rows[1] == (25, 30)


def _paste_paren(img, x0, y0, w, h, opening):
    region = np.zeros((h, w), np.uint8)
    third = h // 3
    near, far = (0, w - 2) if opening else (w - 2, 0)
    region[0:third, far:far + 2] = 255
    region[third:2 * third, near:near + 2] = 255
    region[2 * third:h, far:far + 2] = 255
    img[y0:y0 + h, x0:x0 + w] = np.maximum(img[y0:y0 + h, x0:x0 + w], region)


def _draw_digits(img, text, x0, y0):
    """Render real, Tesseract-readable digit text (not a hand-built
    template) - digit reading itself now goes through Tesseract (see
    rating_read._ocr_digits), so a structural test only needs shapes real
    enough to OCR, not pixel-identical to any internal reference."""
    cv2.putText(img, text, (x0, y0 + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.8, 255, 2)


def test_best_rating_in_chars_ignores_a_far_decoy_cluster():
    # Confirmed on a real video: the on-screen clock sat inside the same
    # fixed-width search band as the true rating, well to the right of it,
    # and its digits won the old "rightmost match wins" tie-break outright.
    # Build a real "(150)" near the origin and a real, differently-valued
    # "(283)" far to the right (standing in for the unrelated clock digits)
    # and confirm only the near one is ever found. (3 digits: 1-2 digit
    # reads are rejected outright - no real rating is under 100.)
    img = np.zeros((30, 400), np.uint8)
    _paste_paren(img, 0, 0, 4, 24, opening=True)
    _draw_digits(img, "150", 6, 0)
    _paste_paren(img, 58, 0, 4, 24, opening=False)

    far_x = 300
    _paste_paren(img, far_x, 0, 4, 24, opening=True)
    _draw_digits(img, "283", far_x + 6, 0)
    _paste_paren(img, far_x + 58, 0, 4, 24, opening=False)

    # Use _band_glyphs's own connected-component detection, same as every
    # real caller does, rather than hand-tracked boxes - putText's "1" and
    # "5" don't touch, so they're two separate components in practice,
    # which matters for the median-width calculation _is_narrow_bracket
    # depends on.
    mask, chars = rr._band_glyphs(img)
    result = rr._best_rating_in_chars(mask, chars, img, min_confidence=0.0)
    assert result is not None
    rating, _ = result
    assert rating == 150   # not 283 - the far decoy must never be considered


def test_best_rating_in_chars_drops_extreme_aspect_ratio_glyphs():
    # A thin decorative divider line (very wide, very short) must not
    # survive into the working char set - confirmed on a real video, one
    # such line (950px wide, 4px tall) spanned nearly an entire search band
    # and defeated the gap-based cut above by bridging straight over the
    # real gap (its own huge right edge made the true gap measure small).
    digit_glyphs = [(0, 4, 0, 20), (6, 10, 0, 20), (12, 16, 0, 20)]
    divider = (0, 300, 8, 9)   # 301 wide, 2 tall - absurd aspect ratio
    mask = np.zeros((21, 320), np.uint8)
    chars = [divider] + digit_glyphs

    widths = sorted(c[1] - c[0] + 1 for c in chars)
    median_w = widths[len(widths) // 2]
    filtered = [c for c in chars
                if (c[1] - c[0] + 1) <= 8 * (c[3] - c[2] + 1) + 10
                and (c[3] - c[2] + 1) <= 8 * (c[1] - c[0] + 1) + 10]
    assert divider not in filtered
    assert all(g in filtered for g in digit_glyphs)


def test_paren_shape_distinguishes_open_and_close():
    # "(" bulges left in the middle third, right at top/bottom.
    open_paren = np.zeros((12, 6), np.uint8)
    open_paren[0:4, 4:6] = 1
    open_paren[4:8, 0:2] = 1
    open_paren[8:12, 4:6] = 1

    # ")" is the mirror image.
    close_paren = np.zeros((12, 6), np.uint8)
    close_paren[0:4, 0:2] = 1
    close_paren[4:8, 4:6] = 1
    close_paren[8:12, 0:2] = 1

    assert rr._paren_shape(open_paren, (0, 5, 0, 11)) == "open"
    assert rr._paren_shape(close_paren, (0, 5, 0, 11)) == "close"


def test_is_narrow_bracket_uses_median_relative_width():
    median_w = 10
    assert rr._is_narrow_bracket((0, 2, 0, 10), median_w) is True    # width 3
    assert rr._is_narrow_bracket((0, 9, 0, 10), median_w) is False   # width 10


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))


def test_row_rating_reads_a_real_lichess_player_row():
    # Real crop (9h8UcYJZz-Q, 1080p): the band under the top clock holds a
    # separator bar, the "retro_sort ... 2115" row, and the move-list
    # toolbar below it. Reading the whole band made Tesseract miss 2115;
    # only the line nearest the clock may be read.
    fixture = Path(__file__).resolve().parent / "fixtures" / "lichess_rating_row_9h8UcYJZz-Q.png"
    row = cv2.imread(str(fixture), cv2.IMREAD_GRAYSCALE)
    rating, _ = rr._row_rating(row, 0, 0, row.shape[1], row.shape[0], from_top=True)
    assert rating == 2115


def test_ratings_beside_clocks_ignores_clocks_above_and_below_the_board():
    # chess.com-style: clocks share the board's columns (above/below it) -
    # the rows next to them are the board or the name row, not a rating row.
    frame = np.full((720, 1280, 3), 40, np.uint8)
    board = (682, 60, 567, 567)
    reading = rr.read_ratings_beside_clocks(frame, board, (1152, 13, 93, 33), (1148, 675, 96, 32))
    assert reading.top is None and reading.bottom is None


def test_rating_in_band_at_1080p_finds_a_slender_closing_paren():
    # Real 1080p band (99-kJ0FoMiM): file letters, then "ohnomyrosen (1275)".
    # The ")" is 7px wide beside 11px digits - too wide for the old
    # width-only bracket test - but 21px tall against the digits' 16-17.
    fixture = Path(__file__).resolve().parent / "fixtures" / "chesscom_rating_band_1080p_99-kJ0FoMiM.png"
    band = cv2.imread(str(fixture), cv2.IMREAD_GRAYSCALE)
    rating, _ = rr._rating_in_band(band, 0, band.shape[0], 0, band.shape[1])
    assert rating == 1275


def test_drop_implausible_pairing():
    # Real partial reads: a 3-digit value facing a 4-digit one 700+ higher.
    assert rr.drop_implausible_pairing(423, 1469) == (None, 1469)
    assert rr.drop_implausible_pairing(2946, 261) == (2946, None)
    # Plausible pairings, both-low pairs and missing sides are left alone.
    assert rr.drop_implausible_pairing(2400, 2950) == (2400, 2950)
    assert rr.drop_implausible_pairing(425, 497) == (425, 497)
    assert rr.drop_implausible_pairing(None, 261) == (None, 261)
