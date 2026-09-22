"""Read on-screen player ratings, e.g. the "(2653)" next to a player's name.

Unlike board recognition and clock reading, this module can't self-calibrate:
a rating never changes during a game, so there's no ticking/wrapping signal
to bootstrap digit shapes from (see clock_read.py's module docstring), and
no motion signal to *locate* the text either (a clock's own ticking is what
find_clock_regions searches by). Both problems needed a different approach:

- Digit *localization* is still bespoke: a positional heuristic (a
  chess.com-style broadcast consistently renders "name (rating)" in a thin
  band directly above/below the board, or - for other layouts - somewhere
  beside it) plus shape/geometry checks (_is_narrow_bracket, _paren_shape)
  to find the "(...)" pattern itself.
- Digit *reading*, once the "(...)" span is found, is Tesseract (via
  pytesseract), not a hand-built template classifier. An earlier version of
  this module hand-built its own digit templates and classified each glyph
  by pixel similarity (see git history) - it hit a real, well-tested
  ceiling: every attempted fix (more templates, more alignment tolerance,
  a topological hole-count filter) traded one video's correctness for
  another's, because a handful of hand-labeled examples can't represent
  every font this module meets. Tesseract, tried head-to-head against every
  hard case gathered during that investigation, got every single one
  exactly right - a decisively better tool for this specific sub-problem,
  not a marginal tweak.

Ratings are the lowest-priority of the three game-data signals this project
extracts (moves, then clocks, then ratings) and approximate correctness is
an explicit, acceptable goal here - unlike clock_read.py, this module does
not need to be conservative to the point of reporting nothing rather than
something occasionally wrong.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import cv2
import numpy as np
import pytesseract

_BAND_HEIGHT = 75     # how far above/below the board to search for rating text -
                       # confirmed on a real video: some broadcast skins render
                       # this row with more vertical padding than others, and a
                       # too-short search band clips the *top* of every glyph
                       # (every char showing y0=0, cut off at the crop edge),
                       # corrupting segmentation into unreadable slivers rather
                       # than failing cleanly. Some margin beyond the tightest
                       # video seen is deliberate, not just enough for one case.
_BAND_PAD = 4         # small margin so text right at the board edge isn't clipped
_MIN_PAREN_GAP = 2    # a run shorter than this can't hold any digits at all


_MIN_AVG_CONFIDENCE = 0.35   # sanity floor (0-1, Tesseract's own 0-100
                             # confidence normalized) on the *winning*
                             # candidate - guards against the case where
                             # nothing in the band is a real rating at all
                             # and every candidate is some flavor of bad


def _ocr_digits(crop: np.ndarray, x0: int, y0: int, x1: int, y1: int
                 ) -> tuple[int, float] | None:
    """OCR the digit span between a candidate "(" and ")", using the
    *original grayscale* pixels (not the thresholded/binary mask) - tried
    both head-to-head on every hard case gathered while this module still
    used hand-built templates: grayscale got 6/6 exactly right, the binary
    mask only 3/6 (thresholding throws away anti-aliasing detail Tesseract's
    model relies on, confirmed worse on a real video, e.g. one case losing
    a whole reading to an empty OCR result). Upscaled generously first -
    Tesseract's models are trained on much larger text than a broadcast
    overlay's rating renders at natively.

    A few pixels of padding around the digit span matters more than it
    looks, and not in a way one fixed value reliably gets right: confirmed
    on a real video, the *same* crop went from misread as letters
    ("ay"/"ted") at 2px padding, to a clean, correct "(2122)" at 6px, to a
    wrong "(21292)" (an extra spurious digit pulled in from a neighbor) at
    5px, 7px, and 8px - correct only at that one specific value, wrong on
    both sides of it. No single fixed padding is safe. Instead of guessing
    one, try a small spread of them and keep whichever gives the highest-
    confidence valid reading - the same "try several, keep the best"
    principle already used for candidate timestamps in scan.py.
    """
    h, w = crop.shape
    best = None
    for pad in (3, 6, 9):
        px0, px1 = max(0, x0 - pad), min(w, x1 + pad + 1)
        py0, py1 = max(0, y0 - pad), min(h, y1 + pad + 1)
        patch = crop[py0:py1, px0:px1]
        if patch.size == 0:
            continue
        big = cv2.resize(patch, None, fx=8, fy=8, interpolation=cv2.INTER_CUBIC)
        data = pytesseract.image_to_data(big, config="--psm 7", output_type=pytesseract.Output.DICT)
        texts = [t for t in data["text"] if t.strip()]
        if not texts:
            continue
        confs = [c for t, c in zip(data["text"], data["conf"]) if t.strip()]
        digits = "".join(ch for t in texts for ch in t if ch.isdigit())
        if not (2 <= len(digits) <= 4):
            continue   # ratings are realistically 2-4 digits (e.g. 40-9999)
        # a 4-digit rating's leading digit can only be 1, 2 or 3 - nobody
        # active is rated 4000+. Only applies to a *4-digit* run: a
        # 3-digit (sub-1000) rating is common early in a speedrun and has
        # no such constraint on its own leading digit (per user,
        # 2026-09-18). This is a hard domain fact, not a soft preference -
        # reject outright.
        if len(digits) == 4 and digits[0] not in ("1", "2", "3"):
            continue
        confidence = (max(confs) / 100.0) if confs else 0.0
        if best is None or confidence > best[1]:
            best = (int(digits), confidence)
    return best


def _is_narrow_bracket(char: tuple[int, int, int, int], median_digit_w: float) -> bool:
    """A "(" or ")" renders much narrower than a digit in every font this
    module has been built against (parens ~4-6px vs. digits ~7-18px at the
    resolutions this module works at)."""
    x0, x1, _, _ = char
    w = x1 - x0 + 1
    return w <= 0.6 * median_digit_w


def _paren_shape(mask: np.ndarray, char: tuple[int, int, int, int]) -> str | None:
    """Tell "(" apart from ")" by which way the glyph curves, not just its
    width - both are "narrow", so width alone can't stop a real ")" from
    being mistaken for a plausible "(" that opens some later, spurious
    match (confirmed on a real video: the true closing paren of a rating
    was reused this way, right before trailing icon glyphs). A "(" bulges
    away from the digits it wraps - its ink is furthest *right* at top and
    bottom and furthest *left* in the middle; ")" is the mirror image."""
    x0, x1, y0, y1 = char
    region = mask[y0:y1 + 1, x0:x1 + 1]
    h = region.shape[0]
    if h < 6:
        return None
    third = max(1, h // 3)

    def center_x(rows: np.ndarray) -> float | None:
        cols = np.nonzero(rows.sum(axis=0))[0]
        return float(cols.mean()) if len(cols) else None

    top_c = center_x(region[:third])
    mid_c = center_x(region[third:h - third])
    bot_c = center_x(region[h - third:])
    if top_c is None or mid_c is None or bot_c is None:
        return None
    edge_c = (top_c + bot_c) / 2
    if mid_c < edge_c - 0.3:
        return "open"
    if mid_c > edge_c + 0.3:
        return "close"
    return None



def _band_glyphs(crop: np.ndarray, thresh: int = 30, adaptive: bool = False
                  ) -> tuple[np.ndarray, list[tuple[int, int, int, int]]] | None:
    """Find individual ink blobs in a crop via connected components, each
    returned as an (x0, x1, y0, y1) box, sorted left to right.

    A search band tall enough to comfortably hold one line of "name
    (rating)" text can also catch unrelated content sitting right next to
    it - confirmed on a real video: a move-number/avatar thumbnail row
    immediately above the name+rating row, overlapping it in row-range but
    not in column-range (the avatar sits to the left of where the name
    starts). A single global tight-ink-bbox pass (column-projection
    segmentation, as clock_read.py's _segment_chars/_tight_ink_bbox do) has
    no way to separate that - it just merges both into one oversized
    bounding box, corrupting every glyph's vertical extent. Connected
    components naturally keep unrelated, non-touching blobs separate
    regardless of how their row ranges overlap.

    `adaptive=True` swaps the single global-median threshold for a locally-
    computed one (cv2.adaptiveThreshold) - needed for a wide, heterogeneous
    search region that spans several differently-colored UI panels at once
    (confirmed on a real video: a sidebar region containing a dark
    background, a lighter name-tag box, and a webcam panel all at once
    thresholded into one giant blob under a single global median, since
    there's no one "background" color for the whole region to compare
    against). Kept off by default since the plain global threshold is both
    faster and more reliable for the common case: a thin band immediately
    beside the board with one dominant background color."""
    if adaptive:
        mask = cv2.adaptiveThreshold(crop, 1, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                      cv2.THRESH_BINARY, 31, -10)
    else:
        bg = float(np.median(crop))
        mask = (np.abs(crop.astype(np.int16) - bg) > thresh).astype(np.uint8)
    num, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    boxes = []
    for i in range(1, num):
        x, y, w, h, area = stats[i]
        if area < 3:
            continue
        boxes.append((x, x + w - 1, y, y + h - 1))
    if not boxes:
        return None
    boxes.sort(key=lambda b: b[0])

    # A photo (an avatar thumbnail) doesn't threshold into one solid blob
    # the way text does - it fragments into several components that
    # overlap each other in x, which breaks any later left-to-right gap
    # analysis that assumes non-overlapping, strictly increasing ranges
    # (confirmed on a real video). Merge any run of mutually-overlapping
    # boxes into one - a real character's box never overlaps its neighbor's,
    # so this only ever collapses non-text clutter like a photo, never
    # merges two genuine adjacent glyphs into each other.
    # Require the two boxes to *also* overlap in y before merging - x-overlap
    # alone was enough for a thin band (nothing unrelated shared its narrow
    # y-range there), but confirmed on a real video: over a wide search
    # region spanning many unrelated elements at different heights (text,
    # webcam feed, banner), x-overlap-only merging cascades across nearly
    # the whole region, chaining together components that just happen to
    # share a column while sitting in completely different rows. A true
    # photo fragment overlaps its sibling fragments in both x and y, so
    # this doesn't weaken the original avatar-merging case at all. Union-
    # find so a multi-hop chain of overlapping fragments (A-B, B-C) still
    # merges fully into one box regardless of scan order.
    n = len(boxes)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        x0, x1, y0, y1 = boxes[i]
        for j in range(i + 1, n):
            x0b, x1b, y0b, y1b = boxes[j]
            if x0 <= x1b and x0b <= x1 and y0 <= y1b and y0b <= y1:
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    chars: list[tuple[int, int, int, int]] = []
    for members in groups.values():
        xs0 = min(boxes[i][0] for i in members)
        xs1 = max(boxes[i][1] for i in members)
        ys0 = min(boxes[i][2] for i in members)
        ys1 = max(boxes[i][3] for i in members)
        chars.append((xs0, xs1, ys0, ys1))
    chars.sort(key=lambda c: c[0])
    return mask, chars


def _best_rating_in_chars(mask: np.ndarray, chars: list[tuple[int, int, int, int]],
                           crop: np.ndarray,
                           min_confidence: float = _MIN_AVG_CONFIDENCE
                           ) -> tuple[int, float] | None:
    """Look for a "(NNNN)" pattern among one line's worth of connected-
    component glyphs (see _band_glyphs / _rating_in_region), and return
    the best (rating, confidence) match if any.

    Tries every pair of narrow (paren-shaped) glyphs as a candidate
    "(...)" and keeps whichever one's contents classify as digits with the
    highest average confidence, rather than just taking the first pair
    that merely parses as a plausible 2-4 digit run. A narrow letter within
    the player's own name (e.g. a double "ll") can coincidentally pair up
    with another narrow glyph and produce *something* that parses - but
    forcing a letter through a digit classifier scores far worse on
    average than real digits do, even accounting for how much an
    imperfectly-segmented real digit's own confidence can vary (confirmed
    on a real video: a name-internal false match beat the real rating
    under a "first match wins" rule, but scored clearly lower on average
    confidence than the real one).

    Only considers the first contiguous cluster of glyphs, stopping at the
    first unusually large horizontal gap - confirmed on a real video: the
    fixed search band beside the board also reached far enough right to
    catch that video's on-screen clock digits, a completely unrelated
    element separated from the name+rating text by a wide blank gap. The
    "rightmost wins" tie-break above made this actively harmful (it
    preferred a bogus match built from clock digits over the real rating,
    simply for being further right) rather than just noise to filter.

    Also drops any glyph with an extreme aspect ratio before any of the
    above - confirmed on a real video: a thin decorative divider line under
    the name/rating row (950px wide, 4px tall) spans nearly the entire
    crop, which not only isn't text but actively defeats the gap-based cut
    above (its own huge right edge makes the *real* gap after it measure
    as small, "bridging" straight over to whatever comes next). No genuine
    letter, digit, or paren in this module has ever rendered anywhere near
    this lopsided."""
    chars = [c for c in chars
             if (c[1] - c[0] + 1) <= 8 * (c[3] - c[2] + 1) + 10
             and (c[3] - c[2] + 1) <= 8 * (c[1] - c[0] + 1) + 10]
    if len(chars) < 3:
        return None

    widths = sorted(c[1] - c[0] + 1 for c in chars)
    median_w = widths[len(widths) // 2]

    max_gap = max(40, 4 * median_w)
    for i in range(1, len(chars)):
        if chars[i][0] - chars[i - 1][1] > max_gap:
            chars = chars[:i]
            break
    if len(chars) < 3:
        return None

    narrow_idxs = [i for i, c in enumerate(chars) if _is_narrow_bracket(c, median_w)]

    # Two-tier selection, in order of trust (this structure predates the
    # switch to Tesseract - see git history - and is kept because the
    # underlying false-positive shapes it defends against are about
    # *localization*, not digit reading, so they apply just as much now):
    #
    # 1. A pair where *both* glyphs have a definitively-confirmed matching
    #    shape (_paren_shape said "open"/"close", not just "not the other
    #    one"). Confirmed on a real video: trailing icons (a flag, a
    #    connection-strength indicator) can be narrow AND shape-ambiguous,
    #    and OCR a plausible-looking short digit run from them - but
    #    neither glyph confirms as a real paren shape, unlike the true
    #    rating's parens right before them.
    # 2. Only if no fully-confirmed pair exists at all: fall back to the
    #    broader set (glyphs merely not-ruled-out by shape) and prefer the
    #    *rightmost* valid match over the most confident one. A "name
    #    (rating)" label always ends with its rating, so a false match on a
    #    narrow letter pair earlier in a long username is structurally to
    #    the left of the real one.
    #
    # Both tiers prefer rightmost over highest-confidence among their own
    # candidates (not just tier 2) - confirmed on a real video, a real
    # digit (a narrow "1") right before the true closing paren can itself
    # coincidentally read as "close"-shaped, making a truncated prefix
    # match also fully shape-confirmed; rightmost reliably picks the real,
    # longer match over that kind of truncation, where confidence alone
    # was shown not to (see git history for the specific averaging-bias
    # mechanism that caused, back when confidence was itself an average
    # over independently-classified digits - Tesseract's confidence isn't
    # built that way, but rightmost is still strictly safe here and costs
    # nothing to keep).
    shapes = {i: _paren_shape(mask, chars[i]) for i in narrow_idxs}
    confirmed_open = [i for i in narrow_idxs if shapes[i] == "open"]
    confirmed_close = [i for i in narrow_idxs if shapes[i] == "close"]

    # A fully shape-confirmed pair earns a lower confidence bar than the
    # fallback tier below - shape alone already does most of the false-
    # positive rejection there, so confidence doesn't have to work as hard.
    _MIN_CONFIRMED_CONFIDENCE = 0.3
    best_rating, best_score, best_confirmed_close = None, -1.0, -1
    for open_idx in confirmed_open:
        for close_idx in confirmed_close:
            if close_idx - open_idx < _MIN_PAREN_GAP:
                continue
            result = _digits_between(crop, chars, open_idx, close_idx)
            if result is None:
                continue
            rating, score = result
            if score >= _MIN_CONFIRMED_CONFIDENCE and close_idx > best_confirmed_close:
                best_rating, best_score, best_confirmed_close = rating, score, close_idx
    if best_rating is not None:
        return best_rating, best_score

    open_idxs = [i for i in narrow_idxs if shapes[i] != "close"]
    close_idxs = [i for i in narrow_idxs if shapes[i] != "open"]
    best_close_idx = -1
    for open_idx in open_idxs:
        for close_idx in close_idxs:
            if close_idx - open_idx < _MIN_PAREN_GAP:
                continue
            result = _digits_between(crop, chars, open_idx, close_idx)
            if result is None:
                continue
            rating, score = result
            if score >= min_confidence and close_idx > best_close_idx:
                best_rating, best_close_idx, best_score = rating, close_idx, score
    return (best_rating, best_score) if best_rating is not None else None


def _rating_in_band(gray: np.ndarray, y0: int, y1: int, x0: int, x1: int
                     ) -> tuple[int, float] | None:
    """_best_rating_in_chars over one fixed horizontal band of a frame -
    but not necessarily treating the whole band as one line.

    A band tall enough to avoid clipping text on some broadcast layouts
    (see _BAND_HEIGHT) can fit an entirely unrelated row within that same
    height on others - confirmed on a real video: a board's own file-
    coordinate labels ("a b c ... h") rendered just outside the board
    edge, stacked above the real name+rating row, both inside one band.
    Cluster into rows and try each rather than assuming the whole band is
    a single line - the same thing _rating_in_region already has to do
    for a sidebar layout's multiple stacked lines."""
    y0, x0 = max(0, y0), max(0, x0)
    crop = gray[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    found = _band_glyphs(crop)
    if found is None:
        return None
    mask, chars = found

    best = None
    for cluster in _row_clusters(chars):
        if len(cluster) < 3:
            continue
        line_chars = sorted((chars[i] for i in cluster), key=lambda c: c[0])
        result = _best_rating_in_chars(mask, line_chars, crop)
        if result is None:
            continue
        if best is None or result[1] > best[1]:
            best = result
    return best


def _row_clusters(chars: list[tuple[int, int, int, int]]) -> list[list[int]]:
    """Group glyph indices into text lines by vertical (y-range) overlap.

    Safe in a way column-projection segmentation never was (see
    _band_glyphs's docstring): this clusters already-separated individual
    glyph boxes, so two unrelated glyphs that happen to share row-range but
    sit in different UI elements only end up in the same cluster if their
    *own* y-ranges genuinely overlap - there's no shared bounding box to
    corrupt. Two text lines stacked with a visible gap don't overlap in y
    at all, so they cluster apart correctly."""
    order = sorted(range(len(chars)), key=lambda i: chars[i][2])
    clusters: list[list[int]] = []
    cur: list[int] = []
    cur_y0, cur_y1 = None, None
    for i in order:
        _, _, y0, y1 = chars[i]
        if cur and y0 > cur_y1:
            clusters.append(cur)
            cur, cur_y0, cur_y1 = [], None, None
        cur.append(i)
        cur_y0 = y0 if cur_y0 is None else min(cur_y0, y0)
        cur_y1 = y1 if cur_y1 is None else max(cur_y1, y1)
    if cur:
        clusters.append(cur)
    return clusters


def _rating_in_region(gray: np.ndarray, y0: int, y1: int, x0: int, x1: int
                       ) -> list[tuple[int, float, float]]:
    """Search a whole region (not just one fixed-height band) for any
    "(NNNN)"-shaped text line, e.g. a sidebar name+rating panel that isn't
    positioned directly above/below the board at all (confirmed on a real
    video: a streamer's own custom broadcast overlay put both players'
    name+rating panels in a column to the *left* of the board, spanning
    close to the full frame height, unlike the standard chess.com
    live-game layout this module was first built against). Returns every
    valid match found, as (rating, confidence, y_center) tuples, letting
    the caller decide which - if any - correspond to the two players."""
    y0, x0 = max(0, y0), max(0, x0)
    crop = gray[y0:y1, x0:x1]
    if crop.size == 0:
        return []
    found = _band_glyphs(crop, adaptive=True)
    if found is None:
        return []
    mask, chars = found

    # A wide search region can contain decorative elements no single text
    # line ever would - confirmed on a real video: a full-height accent
    # bar (h=270) on a sidebar name panel bridged two separate name+rating
    # rows into one bogus cluster, since row-clustering just follows
    # whatever y-range each glyph spans. No real character in this module
    # (digits, parens, or the letters of a player name) has rendered
    # anywhere near this tall at any resolution seen so far - cap out
    # anything that isn't plausibly a single line of text before clustering.
    _MAX_LINE_GLYPH_H = 50
    text_like = [i for i, c in enumerate(chars) if c[3] - c[2] + 1 <= _MAX_LINE_GLYPH_H]

    matches = []
    for cluster in _row_clusters([chars[i] for i in text_like]):
        cluster = [text_like[j] for j in cluster]
        if len(cluster) < 3:
            continue
        line_chars = sorted((chars[i] for i in cluster), key=lambda c: c[0])
        # A confidently-wrong rating is worse than none at all here - the
        # caller can (and does) just exclude an unknown rating from the UI,
        # but a wrong one silently corrupts the catalog. This wide, adaptive-
        # threshold search is noisier than the thin fixed band this
        # module's confidence floor was calibrated against: confirmed on a
        # real video (a streamer's sidebar layout, dim gray rating text
        # over a textured avatar background) that a false match entirely
        # inside the player's own name cleared 0.35 *and* 0.55 - the font
        # in that case fuses the opening "(" directly onto the following
        # digit, so the true pair's own open paren isn't even a separate
        # glyph to find. This floor doesn't fix that (needs real digit-
        # count-aware paren detection to fix properly - not yet done), it
        # just keeps the fallback path failing closed to None rather than
        # emitting a wrong rating when it can't find the real pair.
        result = _best_rating_in_chars(mask, line_chars, crop, min_confidence=0.7)
        if result is None:
            continue
        rating, score = result
        y_center = sum(chars[i][2] + chars[i][3] for i in cluster) / (2 * len(cluster))
        matches.append((rating, score, y_center))
    return matches


def _digits_between(crop: np.ndarray, chars: list[tuple[int, int, int, int]],
                     open_idx: int, close_idx: int) -> tuple[int, float] | None:
    """OCR the span strictly between a candidate "(" and ")" - excludes the
    parens themselves. Tesseract reads the whole span as one string, merged
    or touching digits included, so unlike the old hand-built classifier
    this needs no separate digit-splitting step at all."""
    inside = chars[open_idx + 1:close_idx]
    if not inside:
        return None
    x0 = min(c[0] for c in inside)
    x1 = max(c[1] for c in inside)
    y0 = min(c[2] for c in inside)
    y1 = max(c[3] for c in inside)
    return _ocr_digits(crop, x0, y0, x1, y1)


@dataclass
class RatingReading:
    top: int | None
    bottom: int | None
    # Per-side confidence of the reading above (see _digits_between's
    # avg_confidence), None whenever the corresponding value is None. Lets
    # a caller sampling multiple frames of the same still-static rating
    # (see scan.py.annotate_ratings) prefer the best of several imperfect
    # reads instead of just the first non-None one - confirmed on a real
    # video, an earlier frame's read for the same rating can score lower
    # confidence (and be less accurate) than a later frame's.
    top_confidence: float | None = None
    bottom_confidence: float | None = None


def read_ratings(frame_bgr: np.ndarray, board_box, allow_region_fallback: bool = True) -> RatingReading:
    """Best-effort read of both players' ratings from a single high-
    resolution frame, using the on-screen text right above and below the
    board (the layout a chess.com-style broadcast consistently uses).
    `board_box` needs x, y, w, h attributes or a 4-tuple (as_tuple()).
    Returns None for whichever side (or both) this frame doesn't confidently
    show a rating for - a frame mid-transition, a layout this heuristic
    doesn't cover, or camera/overlay clutter in that band are all expected
    and not distinguished from each other.

    `allow_region_fallback=False` skips the wider sidebar-style search
    below entirely. Confirmed on a real video: the direct band search
    failing on *one* frame doesn't mean this video needs the fallback at
    all - it can just mean a transient state (e.g. a "loading" placeholder
    before matchmaking populates a name) that a *later* frame would read
    fine directly. But the fallback, searching a much wider area, can find
    a confidently-wrong match in unrelated static text elsewhere in frame
    (e.g. a channel's own permanent "N wins - N losses" title card) that
    then blocks a caller's own retry-at-a-later-timestamp logic from ever
    trying again, since a high-confidence wrong answer looks like success.
    A caller that retries across multiple timestamps should pass False
    here and only allow the fallback as a last resort after every direct-
    band attempt across every timestamp has already failed.
    """
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    bx, by, bw, bh = board_box.as_tuple() if hasattr(board_box, "as_tuple") else board_box

    top_r = _rating_in_band(gray, by - _BAND_HEIGHT, by - _BAND_PAD, bx, bx + bw + 100)
    bottom_r = _rating_in_band(gray, by + bh + _BAND_PAD, by + bh + _BAND_HEIGHT, bx, bx + bw + 100)
    top, top_conf = top_r if top_r else (None, None)
    bottom, bottom_conf = bottom_r if bottom_r else (None, None)
    if (top is not None and bottom is not None) or not allow_region_fallback:
        return RatingReading(top=top, bottom=bottom,
                              top_confidence=top_conf, bottom_confidence=bottom_conf)

    # The standard chess.com live-game layout puts "name (rating)" directly
    # above/below the board, but that's not the only broadcast layout in
    # the wild - confirmed on a real video: a streamer's own custom overlay
    # put both players' name+rating panels in a sidebar to the *left* of
    # the board, spanning nearly the full frame height instead. Fall back
    # to a wider, unconstrained search of the space beside the board rather
    # than hardcoding "left" or "right" specifically, so this covers
    # whichever side a given broadcast happens to use.
    frame_h, frame_w = gray.shape
    matches: list[tuple[int, float, float]] = []
    if bx > 100:
        matches += _rating_in_region(gray, 0, frame_h, 0, bx)
    if frame_w - (bx + bw) > 100:
        matches += _rating_in_region(gray, 0, frame_h, bx + bw, frame_w)
    matches.sort(key=lambda m: m[2])   # top to bottom by y-center

    if matches:
        if top is None:
            top, top_conf = matches[0][0], matches[0][1]
        if bottom is None and len(matches) > 1:
            bottom, bottom_conf = matches[-1][0], matches[-1][1]
    return RatingReading(top=top, bottom=bottom,
                          top_confidence=top_conf, bottom_confidence=bottom_conf)


_ROW_RATING_RE = re.compile(r"^(\d{3,4})\??$")   # lichess marks a provisional rating "1500?"


def _row_rating(gray: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> tuple[int, float] | None:
    """The rightmost standalone 3-4 digit number in one row of text."""
    h, w = gray.shape
    x0, x1, y0, y1 = max(0, x0), min(w, x1), max(0, y0), min(h, y1)
    if x1 - x0 < 10 or y1 - y0 < 5:
        return None
    crop = gray[y0:y1, x0:x1]
    big = cv2.resize(crop, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
    data = pytesseract.image_to_data(big, config="--psm 7", output_type=pytesseract.Output.DICT)
    best = None
    for left, text, conf in zip(data["left"], data["text"], data["conf"]):
        m = _ROW_RATING_RE.match(text.strip())
        if not m:
            continue
        digits = m.group(1)
        if len(digits) == 4 and digits[0] not in ("1", "2", "3"):
            continue
        if best is None or left > best[0]:
            best = (left, int(digits), max(float(conf), 0.0) / 100.0)
    return (best[1], best[2]) if best else None


def read_ratings_beside_clocks(frame_bgr: np.ndarray, board_box, upper_clock, lower_clock) -> RatingReading:
    """Read each player's rating from the row of text beside their clock:
    just below the top player's clock and just above the bottom player's -
    lichess's layout ("name ....... 2407" between the clock and the move
    list), where the "(rating)" search above/below the board finds nothing
    or, worse, the game-info panel listing *both* players (confirmed on
    real videos: ratings swapped or garbage on 6 of 7 lichess games).

    Clock boxes are (x, y, w, h) in this frame's pixels. A row that would
    overlap the other clock is skipped - with both clocks stacked in one
    panel (seen on chess.com-style overlays) that row is the other clock,
    and "3:42" read without its colon must not become a rating of 342.
    """
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    w_img = gray.shape[1]
    bx, by, bw, bh = board_box.as_tuple() if hasattr(board_box, "as_tuple") else board_box

    def band(clock, below: bool, other):
        cx, cy, cw, ch = clock
        x0 = cx
        if cx + cw / 2 > bx + bw:          # clock panel right of the board:
            x0 = max(x0, bx + bw + 4)      # keep board squares out of the row
        y0, y1 = (cy + ch, cy + 2 * ch) if below else (cy - ch, cy)
        ox, oy, ow, oh = other
        if y0 < oy + oh and oy < y1 and x0 < ox + ow and ox < w_img:
            return None
        return _row_rating(gray, x0, y0, w_img, y1)

    top = band(upper_clock, True, lower_clock)
    bottom = band(lower_clock, False, upper_clock)
    return RatingReading(top=top[0] if top else None, bottom=bottom[0] if bottom else None,
                         top_confidence=top[1] if top else None,
                         bottom_confidence=bottom[1] if bottom else None)
