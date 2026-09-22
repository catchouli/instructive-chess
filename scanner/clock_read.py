"""Read the two on-screen clocks (one per player) from a video, with zero
external assets - same self-calibrating philosophy as piece recognition in
recognize.py, just applied to clock digits instead of piece icons.

Clock position varies wildly across sites/layouts (anchored to the board's
corners on some broadcasts, in a dedicated panel far from the board on
others) so it can't be hardcoded. Instead we *locate* the two clocks from how
they behave: a countdown clock is one of the only things in the frame that
changes in a small, fixed-position, text-shaped region over time (unlike a
moving face, a scrolling chat, or a static logo/name).

Reading the digits without any font/template assets works in two stages.

First, a bootstrap: a countdown's fastest-changing digit (ones of seconds)
cycles through 0-9 in a fixed, known, descending order once every ten real
seconds. Rather than clustering glyph images into 10 shape-based buckets up
front - fragile, since real per-frame renders of the very same digit are
noisy enough (compression, sub-pixel jitter) to fragment or blend together -
the value is derived from *timing*: counting "did this position's glyph
change from the previous frame" gives a running tick count, and the one
frame where the ones digit wraps 9->8->...->0 exactly overlaps a
tens-of-seconds change, anchoring that tick count to an absolute value with
no external reference needed. That gives us a handful of confidently-known
example glyphs per digit, averaged into a clean template set.

Second, reading: rather than classifying each position once per transition
and trusting that single classification until the next change - which lets
one bad frame corrupt every reading downstream of it until the next real
change happens to correct it - every frame in a run independently guesses
the clock's value at the *start* of that run (its own template-classified
value, offset by its own precise tick count since the run began), and the
run's actual value is whichever guess the most frames agree on. A handful
of misclassified frames are just outvoted; they no longer get to corrupt
the whole run. Ticking (same/different) is a much easier and more reliable
judgment than 10-way digit classification, so this pushes nearly all of the
remaining error into "which exact digit is this", where majority voting can
actually clean it up.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import cv2
import numpy as np
import pytesseract

_CHANGE_LO = 0.10   # a ticking clock changes less than 100% of frame-steps
_CHANGE_HI = 0.85   # (only one side's clock runs at a time) but more than ~0
_DIFF_THRESH = 20   # per-pixel gray delta counted as "changed"

_GLYPH_H = 28            # every digit glyph is rescaled to this height for comparison -
                         # a 16px canvas measurably lost enough detail to confuse a real
                         # "0"/"8" pair (matching hole count, close ZNCC); this is plenty
                         # for typical on-screen clock digit sizes without being slow
_GLYPH_W = 24            # then padded/cropped to this width, preserving aspect ratio -
                         # a thin "1" and a wide "0" must stay visually distinct, so
                         # width is never stretched to fit like height is
_SAME_SIM_DEFAULT = 0.85   # fallback "same digit" threshold when a clock's own
                           # data doesn't give _calibrate_same_sim enough to work with


@dataclass
class ClockBox:
    x: int
    y: int
    w: int
    h: int

    def crop(self, frame_bgr: np.ndarray) -> np.ndarray:
        return frame_bgr[self.y:self.y + self.h, self.x:self.x + self.w]


def find_clock_regions(frames: list[np.ndarray], board_box) -> list[ClockBox]:
    """Locate up to 2 candidate clock widgets from a sequence of frames spaced
    ~1s apart, spanning at least a a minute or so of real gameplay (so both
    players' clocks get a chance to tick - only one runs at a time)."""
    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames]
    freq = np.zeros_like(grays[0], dtype=np.float32)
    for a, b in zip(grays, grays[1:]):
        freq += np.abs(a.astype(np.int16) - b.astype(np.int16)) > _DIFF_THRESH
    freq /= max(len(grays) - 1, 1)

    mask = ((freq > _CHANGE_LO) & (freq < _CHANGE_HI)).astype(np.uint8) * 255
    bx, by, bw, bh = board_box.as_tuple() if hasattr(board_box, "as_tuple") else board_box
    pad = 10
    y0, y1 = max(0, by - pad), by + bh + pad
    x0, x1 = max(0, bx - pad), bx + bw + pad
    mask[y0:y1, x0:x1] = 0    # exclude the board itself - a piece moving looks like "change" too

    # bridge a single ticking digit into the full "M:SS" label's width
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 25), np.uint8))
    num, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)

    ref = grays[len(grays) // 2]
    h_img, w_img = ref.shape
    pad = 6
    cands = []
    for i in range(1, num):
        x, y, w, h, area = stats[i]
        # A clock's *tens*-of-seconds digit only changes once every ~10
        # ticks; a player whose turns come in short, scattered bursts
        # (confirmed on a real video: ~26 ticks total spread across a
        # 7-minute game) can go the whole window without a single tens
        # rollover landing between two consecutive sampled frames, leaving
        # only the ones-of-seconds digit's own single-character blob ever
        # clearing the frequency threshold - narrower and closer to square
        # than a real two-digit "SS" pair. Keeping the width/aspect gate
        # tight enough for a normal two-digit blob silently dropped this
        # clock's only candidate before segmentation ever got a chance to
        # validate it - loosened to admit a single digit; segmentation
        # (below) is the real filter against non-clock UI clutter, not size.
        if area < 60 or w < 10 or h < 10 or h > 60:
            continue
        aspect = w / max(h, 1)
        if not (0.4 <= aspect <= 10.0):
            continue
        # Validate by segmentation, not just size/aspect: a webcam or other
        # moving UI clutter can easily match the size/shape filters above by
        # coincidence, but won't cleanly segment into a plausible clock
        # character count the way real "M:SS"/"MM:SS"/"H:MM:SS" text does.
        #
        # Try a modest pad first, then progressively wider ones to the left
        # only if that fails: the blob itself is sometimes narrower than the
        # full clock label - the frequency mask only lights up pixels that
        # change often enough to clear _CHANGE_LO, and a slower-moving part
        # of the label (a minutes digit on a long time control, or even the
        # tens-of-seconds digit for a player whose turns come in short,
        # scattered bursts) can change far too rarely over a whole game's
        # worth of frames to ever qualify - leaving only whichever part
        # ticks fastest (confirmed on two real videos: a 149px-wide "09:41"
        # localized as just a 54px seconds-pair blob when only the minutes
        # digit was too slow; a 22px single ones-of-seconds digit when the
        # tens digit was too, on a clock that only ticked ~26 times across
        # the whole window). How much padding recovers the rest scales with
        # how much of the label is already missing - a lone digit needs
        # several multiples of its own width to reach all the way back to
        # the minutes column, not just enough for one adjacent digit - so
        # widen in escalating steps rather than jumping straight to (or
        # stopping at) one fixed guess. Trying wider pads only as a fallback
        # - not unconditionally - avoids overshooting into a neighboring UI
        # element (or the board itself, invisible to this pass since only
        # the frequency mask excludes it) on the common case where the
        # narrow pad already had the whole label.
        found = None
        for left_pad in (pad, *(max(pad, m * w) for m in (2, 3, 4, 6, 8))):
            px0, py0 = max(0, x - left_pad), max(0, y - pad)
            px1, py1 = min(w_img, x + w + pad), min(h_img, y + h + pad)
            crop = ref[py0:py1, px0:px1]
            res = _tight_ink_bbox(crop)
            if res is None:
                continue
            mask, bbox = res
            chars = _strip_leading_icon(_strip_decimal_tenths(mask, _segment_chars(mask, bbox)))
            # A character count alone is too weak a signature once the
            # size/aspect gate above is loose enough to admit a single
            # digit: plenty of non-clock UI text (a scoreboard, a move
            # counter, chat) can coincidentally segment into 4-5 blobs too,
            # especially once the escalating wide-pad fallback is free to
            # grow the crop across a big stretch of the frame (confirmed on
            # a real video: two sizeable false candidates elsewhere on
            # screen outranked - by pixel area - the real, much smaller
            # clock once this loosening was in place, and clock_read.py had
            # no way to tell them apart). A colon is a much more specific
            # signature real clock text always has and generic UI text
            # essentially never coincidentally produces - require at least
            # one segmented character to look like one.
            if len(chars) in (4, 5, 7) and any(_is_colon(mask, c) for c in chars):
                found = (px0, py0, px1, py1)
                break
        if found is None:
            continue
        px0, py0, px1, py1 = found
        cands.append((area, ClockBox(px0, py0, px1 - px0, py1 - py0)))
    cands.sort(key=lambda t: -t[0])
    return [box for _, box in cands[:2]]


def _tight_ink_bbox(gray: np.ndarray, margin: int = 4, thresh: int = 40):
    """Trim a loose widget crop down to the tight bounding box of its text,
    ignoring the rounded-corner/border pixels near the edges.

    A fixed pixel margin isn't enough: the widget's own border/frame line can
    be thicker than the margin (or the crop can include extra context), and
    unlike real glyph ink - which always leaves gaps and never reaches every
    row/column - a border runs edge-to-edge. So after the coarse margin trim,
    peel off any remaining row/column that's mostly flagged; that's frame,
    not text.
    """
    h, w = gray.shape
    m = min(margin, h // 3, w // 3)
    inner = gray[m:h - m, m:w - m] if m > 0 else gray
    if inner.size == 0:
        return None
    bg = float(np.median(inner))
    mask = (np.abs(inner.astype(np.int16) - bg) > thresh)

    ih, iw = mask.shape
    top, bot, left, right = 0, ih, 0, iw
    changed = True
    while changed and bot - top > 2 and right - left > 2:
        changed = False
        if mask[top, left:right].mean() > 0.7:
            top += 1
            changed = True
        if mask[bot - 1, left:right].mean() > 0.7:
            bot -= 1
            changed = True
        if mask[top:bot, left].mean() > 0.7:
            left += 1
            changed = True
        if mask[top:bot, right - 1].mean() > 0.7:
            right -= 1
            changed = True
    mask = mask[top:bot, left:right]

    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    return mask, (xs.min(), xs.max(), ys.min(), ys.max())


def _is_colon(mask: np.ndarray, char: tuple[int, int, int, int]) -> bool:
    """A colon is two dots with a real gap between them: somewhere between
    its topmost and bottommost ink, at least one full row has none at all.
    A digit's ink, even a hollow one like "0", is one continuous vertical
    stroke - every row from its top to its bottom has *some* ink, just not
    always the same columns. This is a purely topological check (does the
    ink region have an internal gap or not), which held up cleanly across
    two very different clock fonts; an earlier version compared the overall
    fraction of rows with ink to a fixed threshold; and that number turned
    out to depend heavily on how tall the colon's dots render relative to
    the digit height on a given clock, so what counted as "enough gap" on
    one font wrongly flagged a real colon as a digit on another."""
    x0, x1, y0, y1 = char
    region = mask[y0:y1 + 1, x0:x1 + 1]
    ink_rows = region.sum(axis=1) > 0
    nz = np.nonzero(ink_rows)[0]
    if len(nz) == 0:
        return False
    first, last = nz[0], nz[-1]
    return not bool(ink_rows[first:last + 1].all())


def _is_decimal_point(mask: np.ndarray, char: tuple[int, int, int, int]) -> bool:
    """A decimal point is a small dot sitting near the bottom of the row,
    nowhere close to spanning the full digit height - unlike a colon (two
    dots that together span most of a digit's height, just with a gap in
    the middle) or any digit (one continuous stroke top to bottom). Some
    clocks switch to a "M:SS.T" tenths-of-a-second display once time gets
    low (confirmed on a real video, chess.com, under ~4 seconds remaining);
    this is purely a shape check, so it doesn't care what comes before or
    after it - the caller decides what a trailing period means."""
    x0, x1, y0, y1 = char
    row_h = y1 - y0 + 1
    region = mask[y0:y1 + 1, x0:x1 + 1]
    ink_rows = np.nonzero(region.sum(axis=1) > 0)[0]
    if len(ink_rows) == 0:
        return False
    ink_h = ink_rows[-1] - ink_rows[0] + 1
    near_bottom = ink_rows[-1] >= 0.7 * row_h
    return bool(ink_h <= 0.4 * row_h and near_bottom)


def _strip_decimal_tenths(mask: np.ndarray, chars: list[tuple[int, int, int, int]]
                           ) -> list[tuple[int, int, int, int]]:
    """Drop a trailing "period, tenths digit" pair so the rest of this
    module can keep treating every clock as plain "M:SS"/"MM:SS"/"H:MM:SS" -
    whole-second precision is all this module ever reports anyway (see
    ClockReading), so a fractional tail is pure noise to strip, not a
    format to parse."""
    if len(chars) >= 2 and _is_decimal_point(mask, chars[-2]):
        return chars[:-2]
    return chars


_ICON_GAP_RATIO = 4.0   # how much wider than the next-widest gap a leading
                        # icon's own gap must be before it's stripped


def _strip_leading_icon(chars: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """Drop a leading character separated from the rest by a gap much wider
    than the normal spacing between real clock characters.

    Some clocks show a small ticking-clock icon well to the left of the
    actual "M:SS" text while that side is active (confirmed on a real
    video, chess.com) - it isn't part of the clock reading, and its own
    rendered width varies a lot frame to frame (sometimes the whole icon,
    sometimes just a sliver of it survives the crop), so it can't be told
    apart from a real leading minutes digit by width or shape alone. The
    *gap* before it is the reliable signal: a consistent ~38-60px margin
    before the real text on that same video, dwarfing the 2-3px gaps
    between the real characters themselves - a relative comparison (this
    gap vs. the largest other gap) rather than a fixed pixel cutoff, the
    same style of check _gap_threshold already uses elsewhere in this
    module, so it isn't tied to one clock's exact font size."""
    if len(chars) < 3:
        return chars
    gaps = [chars[i + 1][0] - chars[i][1] for i in range(len(chars) - 1)]
    # an absolute floor alongside the ratio guards against the degenerate
    # case where every other gap happens to be ~0 (adjacent/touching
    # glyphs), which would otherwise make almost any gap look relatively
    # "huge" by comparison
    if gaps[0] >= max(10, _ICON_GAP_RATIO * max(gaps[1:])):
        return chars[1:]
    return chars


def _segment_chars(mask: np.ndarray, bbox, min_gap: int = 0, min_width: int = 2):
    """Split a tight ink mask into per-character column ranges using vertical
    projection gaps (classic OCR segmentation - works well for the roughly
    fixed-pitch digits a clock renders)."""
    x0, x1, y0, y1 = bbox
    region = mask[y0:y1 + 1, x0:x1 + 1]
    colsum = region.sum(axis=0)
    ink = colsum > 0
    chars = []
    start = None
    gap = 0
    for i, v in enumerate(ink):
        if v:
            if start is None:
                start = i
            gap = 0
        else:
            if start is not None:
                gap += 1
                if gap > min_gap:
                    end = i - gap
                    if end - start + 1 >= min_width:
                        chars.append((start, end))
                    start = None
    if start is not None:
        end = len(ink) - 1
        if end - start + 1 >= min_width:
            chars.append((start, end))
    return [(x0 + a, x0 + b, y0, y1) for a, b in chars]


@dataclass
class ClockReading:
    t: float
    seconds: float | None


def _char_glyph(mask: np.ndarray, char: tuple[int, int, int, int]) -> np.ndarray:
    """Extract one segmented character as a fixed-size binary image, so
    glyphs of the same digit compare equal regardless of the exact crop.

    Crops a native-scale window *centered on the character*, sized so that
    scaling it to the canvas height also lands it at the canvas width - then
    does exactly one resize, of that fixed-size window, to the canvas.
    Deliberately not "resize the detected bbox to fit the canvas": the ink
    threshold that finds a character's left/right edges is noisy enough
    frame to frame (anti-aliasing, compression) that its measured *width*
    can vary several pixels for the very same digit. Rescaling to that
    noisy width turns a few pixels of edge jitter into a visible size
    distortion of the whole glyph; cropping a fixed window around the
    center and resizing *that* by one clock-wide constant factor doesn't -
    the same real ink lands in the same place regardless of exactly where
    the detector drew the edge that frame.
    """
    x0, x1, y0, y1 = char
    h = y1 - y0 + 1
    scale = _GLYPH_H / max(h, 1)
    native_w = max(1, round(_GLYPH_W / scale))
    cx = (x0 + x1) / 2.0
    sx0 = int(round(cx - native_w / 2))
    sx1 = sx0 + native_w
    img_h, img_w = mask.shape
    csx0, csx1 = max(0, sx0), min(img_w, sx1)
    patch = np.zeros((h, native_w), np.uint8)
    if csx1 > csx0:
        patch[:, csx0 - sx0:csx1 - sx0] = mask[y0:y1 + 1, csx0:csx1].astype(np.uint8) * 255
    resized = cv2.resize(patch, (_GLYPH_W, _GLYPH_H), interpolation=cv2.INTER_AREA)
    # A light blur trades a little shape precision for a lot of stability:
    # measured on a real video, the very same digit's exact sub-pixel
    # rendering shifted enough between frames (whenever neighboring text -
    # e.g. the minutes column - changed width) to score as low as 0.68
    # against itself on raw pixels, comfortably in "different digit"
    # territory. Blurring smooths over that jitter while a real digit's
    # distinct shape still shows through.
    return cv2.GaussianBlur(resized, (3, 3), 0.8)


def _zncc(a: np.ndarray, b: np.ndarray) -> float:
    af, bf = a.astype(np.float32), b.astype(np.float32)
    af, bf = af - af.mean(), bf - bf.mean()
    num = float((af * bf).sum())
    den = float(np.sqrt((af * af).sum() * (bf * bf).sum()))
    return num / den if den > 1e-6 else 0.0


_ALIGN_SHIFT = 1   # max pixels of sub-pixel jitter tolerance in _similarity


def _similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Zero-normalized cross-correlation between two glyph images, tolerant
    of a small sub-pixel alignment shift between them.

    Raw pixel agreement is a bad metric here: a glyph canvas is mostly
    black background, so any two glyphs already agree on most pixels
    before the actual digit shape is considered at all, making genuinely
    different digits look deceptively "similar". Mean-centering (as
    recognize.py already does for piece templates) cancels that shared
    background out and compares the actual ink pattern instead.

    Plain ZNCC is still very position-sensitive, though: a digit's exact
    sub-pixel rendering can shift by a pixel between frames just because the
    surrounding text (e.g. the minutes column) changed width, with the
    digit itself never actually changing - measured on a real video, the
    very same "4" scored as low as 0.68 against itself, comfortably in
    "different digit" territory, from this alone. So try a small range of
    (dx, dy) offsets and take the best-aligned score - a real digit change
    won't accidentally line up this well at any offset, but the same digit
    shifted by a pixel will.
    """
    best = _zncc(a, b)
    h, w = a.shape
    for dx in range(-_ALIGN_SHIFT, _ALIGN_SHIFT + 1):
        for dy in range(-_ALIGN_SHIFT, _ALIGN_SHIFT + 1):
            if dx == 0 and dy == 0:
                continue
            shifted = np.zeros_like(b)
            sx0, sx1 = max(0, dx), min(w, w + dx)
            tx0, tx1 = max(0, -dx), min(w, w - dx)
            sy0, sy1 = max(0, dy), min(h, h + dy)
            ty0, ty1 = max(0, -dy), min(h, h - dy)
            shifted[ty0:ty1, tx0:tx1] = b[sy0:sy1, sx0:sx1]
            best = max(best, _zncc(a, shifted))
    return best


def _segments(times: list[float]) -> list[int]:
    """Group frame indices into runs where consecutive timestamps are close
    enough that "the glyph changed" reliably means "the clock ticked once",
    not "several unobserved seconds - or a completely different value from a
    dropped frame - passed in between"."""
    dts = [b - a for a, b in zip(times, times[1:])]
    median_dt = float(np.median(dts)) if dts else 1.0
    max_gap = max(2.5 * median_dt, median_dt + 1.0)
    seg = [0] * len(times)
    for i in range(1, len(times)):
        dt = times[i] - times[i - 1]
        seg[i] = seg[i - 1] + (0 if 0 < dt <= max_gap else 1)
    return seg


def _calibrate_same_sim(glyphs: list[np.ndarray], seg_id: list[int],
                         default: float = _SAME_SIM_DEFAULT,
                         floor: float = 0.035, ratio: float = 4.0) -> float:
    """Find this clock's own "same digit" similarity threshold instead of
    assuming one font-independent constant.

    No single fixed threshold works across fonts: a plain sans-serif clock's
    genuinely-different digits can score as low as -0.1 ZNCC, while a
    7-segment/LCD-style clock's "5" and "6" - one stroke apart - can score
    0.86 even though they're different digits, comfortably above a generic
    0.80 cutoff (that specific confusion silently ate every 5<->6 transition
    on one real video before this was added). But within *one* clock, the
    "genuinely unchanged" pairs (repeats/pauses) cluster very close to 1.0,
    clearly separated from every "changed" pair below them - so sort this
    clock's own consecutive-pair similarities and find that separation (see
    _gap_threshold).
    """
    sims = [_similarity(glyphs[i], glyphs[i - 1])
            for i in range(1, len(glyphs)) if seg_id[i] == seg_id[i - 1]]
    return _gap_threshold(sims, default, floor, ratio)


def _gap_threshold(sims: list[float], default: float, floor: float, ratio: float) -> float:
    """The pure math behind _calibrate_same_sim, split out so it's directly
    testable without needing glyph images that hash to particular
    similarities.

    Neither "first gap over some fixed size" nor "single largest gap
    anywhere" holds up: a fixed size small enough not to trip on the "same
    digit" cluster's own internal jitter is often *bigger* than the true
    boundary gap on some real clocks, while the "different digit" cluster
    can be wide enough that gaps far from the true boundary, deep in its own
    spread, are *larger* than the boundary gap itself - measured on real
    clocks, both failure modes actually happen. What's consistent is
    *relative*: scanning down from the top,
    the boundary gap dwarfs every gap seen so far inside the tight cluster
    above it. So track the largest internal gap seen so far and stop at the
    first gap that clears both an absolute floor and `ratio` times that
    running max - a small, gradually-growing spread doesn't trigger it, only
    a sudden jump well outside the pattern established so far.
    """
    if len(sims) < 6:
        return default
    s = sorted(sims, reverse=True)
    max_internal = 0.0
    for a, b in zip(s, s[1:]):
        gap = a - b
        if gap > max(floor, ratio * max_internal):
            return (a + b) / 2
        max_internal = max(max_internal, gap)
    return default


def _relative_ticks(glyphs: list[np.ndarray], seg_id: list[int],
                     same_thresh: float) -> tuple[list[int], list[bool]]:
    """Count "did this glyph change from the previous frame" within each
    reliably-spaced run, resetting at every segment boundary.

    Same/different is a much more robust judgment than classifying which of
    10 digits a glyph shows (a real digit's render is noisy enough that two
    frames of the *same* digit can differ almost as much as two different
    digits do in absolute terms - but they still look far more like each
    other than like anything else, which is all a threshold needs) - as long
    as that threshold is calibrated to this clock's own font (see
    _calibrate_same_sim).
    """
    n = len(glyphs)
    ticks = [0] * n
    changed = [False] * n
    for i in range(1, n):
        reliable = seg_id[i] == seg_id[i - 1]
        diff = _similarity(glyphs[i], glyphs[i - 1]) < same_thresh
        changed[i] = reliable and diff
        ticks[i] = (ticks[i - 1] + (1 if diff else 0)) if reliable else 0
    return ticks, changed


def _bootstrap_ones_digits(ones_glyphs: list[np.ndarray], tens_glyphs: list[np.ndarray],
                            seg_id: list[int], ones_thresh: float, tens_thresh: float) -> list[int | None]:
    """Derive an absolute digit value (0-9) for as many ones-of-seconds
    frames as possible from pure timing, without classifying glyph shape at
    all: a countdown's ones digit decrements by exactly one every real
    second, wrapping 9->8->...->0->9 forever, and the tens-of-seconds digit
    only ever changes at the exact instant of that wrap. So the frame right
    before ones and tens change *together* is "0" - a self-contained anchor.

    Every such wrap in a segment is used, and each frame is labeled from
    whichever wrap is nearest to it in tick-distance - not just the first
    wrap found for that whole segment. A single same/different misjudgment
    anywhere in _relative_ticks silently shifts every label's tick count
    from that point on by a constant (nothing about the resulting sequence
    looks wrong locally - it still counts down cleanly, just relative to the
    wrong absolute value), and that error keeps compounding the further you
    get from the anchor it's measured against. Re-anchoring at every wrap
    (they come roughly every 10 ticks) caps how far any single frame's label
    can drift, rather than letting one early error miscalibrate an entire
    multi-minute run.

    This only needs to succeed often enough to harvest a handful of
    confidently-known example glyphs per digit (see _build_templates) - the
    actual reading later on doesn't depend on this reaching every frame.

    ones_thresh and tens_thresh are calibrated separately (see
    _calibrate_same_sim) rather than sharing one threshold: they're
    different glyph positions with their own font-confusion pairs, and a
    threshold tuned for one can be badly miscalibrated for the other (a
    threshold too low for tens's own gap made it register a "change" on
    nearly every frame instead of the ~1-in-10 a real wrap should produce,
    fabricating a wrap-anchor almost every tick).
    """
    ticks, _ = _relative_ticks(ones_glyphs, seg_id, ones_thresh)
    anchors_by_seg = _wrap_anchors(ones_glyphs, tens_glyphs, seg_id, ones_thresh, tens_thresh)

    values: list[int | None] = [None] * len(ones_glyphs)
    for i in range(len(ones_glyphs)):
        candidates = anchors_by_seg.get(seg_id[i])
        if not candidates:
            continue
        nearest_tick, _ = min(candidates, key=lambda a: abs(ticks[i] - a[0]))
        values[i] = (nearest_tick - ticks[i]) % 10
    return values


def _wrap_anchors(ones_glyphs: list[np.ndarray], tens_glyphs: list[np.ndarray], seg_id: list[int],
                   ones_thresh: float, tens_thresh: float) -> dict[int, list[tuple[int, int]]]:
    """The frame right before ones and tens change *together* is "0" - find
    every such wrap-anchor per segment (see _bootstrap_ones_digits, which
    uses this to label digits; read_clock also uses it directly to decide
    which segments are trustworthy enough to contribute to the shared
    templates - see the note there).

    Returns each anchor as (tick, frame index of the "before" glyph) rather
    than just the tick value - _corrected_ticks needs the frame index too,
    to count real tens-digit changes between two anchors directly instead
    of inferring how many wraps occurred from the tick gap alone."""
    ticks, ones_changed = _relative_ticks(ones_glyphs, seg_id, ones_thresh)
    _, tens_changed = _relative_ticks(tens_glyphs, seg_id, tens_thresh)

    raw_by_seg: dict[int, list[tuple[int, int]]] = {}   # seg -> [(anchor tick, frame idx of "before" glyph)]
    for i in range(1, len(ones_glyphs)):
        if ones_changed[i] and tens_changed[i]:
            raw_by_seg.setdefault(seg_id[i - 1], []).append((ticks[i - 1], i - 1))

    anchors_by_seg: dict[int, list[tuple[int, int]]] = {}
    for seg, cands in raw_by_seg.items():
        # "ones and tens changed together" isn't proof of a genuine 9->0
        # wrap on its own: a dropped-frames hiccup in the source stream can
        # make the on-screen clock skip several seconds between two
        # consecutive sampled frames, which just as easily flips both
        # digits at once (measured on a real video: "09:41" -> "09:36" in
        # one frame step, tripping this exact check) without it being a
        # wrap at all. A genuine wrap's "before" glyph is always the same
        # shape - literally a "0" - across every true wrap in a segment,
        # while a skip's "before" glyph is whatever digit the skip happened
        # to land on; with enough candidates to compare, the real wraps
        # form the majority shape-cluster and a skip-induced impostor
        # stands out as a minority of its own. Too few candidates to call a
        # majority are left alone here; the >=2-anchor trust filter in
        # read_clock is the backstop for those.
        if len(cands) >= 3:
            glyphs = [ones_glyphs[idx] for _, idx in cands]
            biggest = max(_cluster_by_similarity(glyphs, ones_thresh), key=len)
            cands = [cands[i] for i in biggest]
        thinned: list[tuple[int, int]] = []
        for tick, idx in sorted(cands):
            if not thinned or tick - thinned[-1][0] >= 8:
                thinned.append((tick, idx))
        anchors_by_seg[seg] = thinned
    return anchors_by_seg


def _cluster_by_similarity(glyphs: list[np.ndarray], same_thresh: float) -> list[list[int]]:
    """Greedily group glyph indices by mutual similarity - nearest existing
    cluster mean if it's close enough, else start a new cluster - the same
    running-average approach classification itself uses. Returns each
    cluster's member indices (into `glyphs`), in creation order."""
    means: list[np.ndarray] = []
    counts: list[int] = []
    members: list[list[int]] = []
    for idx, g in enumerate(glyphs):
        gf = g.astype(np.float32)
        best_i, best_sim = -1, -1.0
        for i, m in enumerate(means):
            sim = _similarity(gf, m)
            if sim > best_sim:
                best_sim, best_i = sim, i
        if best_i >= 0 and best_sim >= same_thresh:
            n = counts[best_i]
            means[best_i] = (means[best_i] * n + gf) / (n + 1)
            counts[best_i] += 1
            members[best_i].append(idx)
        else:
            means.append(gf)
            counts.append(1)
            members.append([idx])
    return members


def _build_templates(glyphs: list[np.ndarray], values: list[int | None],
                      same_thresh: float) -> list[np.ndarray | None]:
    """Build one clean template per digit 0-9, to reuse (via nearest-match)
    for every other position on the same clock.

    Blindly averaging every glyph labeled as a given digit isn't safe: the
    timing bootstrap is very good but not perfect, and with only a handful
    of examples for some digits (a short clip won't necessarily observe each
    value equally often), even one or two mislabeled glyphs can visibly
    blend a wrong digit's shape into the average. So within each digit's
    labeled examples, cluster by mutual similarity first and average only
    the largest cluster - mislabeled examples are rejected the same way a
    misclassification anywhere else in this module is (see _relative_ticks,
    _vote_for_anchors): outvoted, not blended in.
    """
    by_digit: list[list[np.ndarray]] = [[] for _ in range(10)]
    for g, v in zip(glyphs, values):
        if v is not None:
            by_digit[v].append(g)
    return [_largest_cluster_mean(cands, same_thresh) if cands else None for cands in by_digit]


def _largest_cluster_mean(glyphs: list[np.ndarray], same_thresh: float) -> np.ndarray:
    """Greedily group glyphs by mutual similarity and return the mean of
    whichever cluster ends up largest."""
    members = _cluster_by_similarity(glyphs, same_thresh)
    biggest = max(members, key=len)
    return np.mean([glyphs[i] for i in biggest], axis=0).astype(np.uint8)


_CLASSIFY_TIE_MARGIN = 0.05   # candidates within this much of the top ZNCC score
                              # are close enough to break the tie topologically


def _hole_count(glyph: np.ndarray, thresh: int = 100) -> int:
    """Count fully-enclosed background regions in a glyph: "0" and "6" and
    "9" have one, "8" has two, the rest have none. ZNCC compares raw pixel
    correlation and can be fooled by two round, similarly-sized digits (0
    vs 8 is the pair that's actually come up); this topological count is a
    cheap, independent signal that doesn't share that blind spot."""
    binary = (glyph > thresh).astype(np.uint8)
    bg = 1 - binary
    num, labels = cv2.connectedComponents(bg)
    border = (set(labels[0, :].tolist()) | set(labels[-1, :].tolist())
              | set(labels[:, 0].tolist()) | set(labels[:, -1].tolist()))
    return sum(1 for lbl in range(1, num) if lbl not in border)


def _classify(glyph: np.ndarray, templates: list[np.ndarray | None],
              valid_digits: range | None = None) -> int | None:
    """Nearest-template match, optionally restricted to a set of digits that
    could ever legitimately appear here - e.g. tens-of-seconds is always
    0-5 (seconds only run 00-59), a free, purely mathematical constraint
    that costs nothing to enforce and rules out an entire class of
    misreads no amount of better image processing would otherwise catch."""
    sims = sorted(((d, _similarity(glyph, t)) for d, t in enumerate(templates)
                    if t is not None and (valid_digits is None or d in valid_digits)),
                  key=lambda p: -p[1])
    if not sims:
        return None
    best_d, best_sim = sims[0]
    # Only worth breaking a tie among candidates ZNCC couldn't clearly
    # separate - and only when hole-count picks out exactly one of them,
    # not zero (no better answer) or more than one (still ambiguous).
    tied = [d for d, s in sims if best_sim - s <= _CLASSIFY_TIE_MARGIN]
    if len(tied) > 1:
        q_holes = _hole_count(glyph)
        matches = [d for d in tied if _hole_count(templates[d]) == q_holes]
        if len(matches) == 1:
            return matches[0]
    return best_d


_MIN_SEG_VOTES = 3       # a segment needs at least this many votes to trust its winner
_MIN_VOTE_FRAC = 0.6     # ...and the winner must hold a clear (not bare) majority of them


_VOTE_TOL = 3   # guesses within this many seconds of each other count as the same vote


def _cluster_votes(guesses: list[int], tol: int = _VOTE_TOL) -> dict[int, int]:
    """Group raw per-frame anchor guesses that land within `tol` of each
    other, so small residual jitter doesn't split one real majority into
    several similar-but-not-identical guesses that individually look like
    minorities. Returns {representative_value: vote_count} - same shape
    _vote_for_anchors already expects, just pre-merged.

    Splits on gaps between *consecutive* sorted values, not distance from a
    fixed representative - comparing everything to whichever value a
    cluster happened to see first misses a jitter chain that drifts
    gradually (591, 592, 593, ... 601) further than `tol` end-to-end, even
    though every adjacent step is well within it.
    """
    if not guesses:
        return {}
    s = sorted(guesses)
    clusters = [[s[0]]]
    for g in s[1:]:
        if g - clusters[-1][-1] <= tol:
            clusters[-1].append(g)
        else:
            clusters.append([g])
    return {round(sum(c) / len(c)): len(c) for c in clusters}


def _vote_for_anchors(votes: dict[int, dict[int, int]]) -> dict[int, int]:
    """Pick each segment's winning anchor guess, only where the majority is
    both large enough in absolute terms and decisive enough as a share of
    that segment's votes - a 2-1 "win" on 3 frames isn't trustworthy, and
    neither is a 51-49 split on 100."""
    seg_anchor: dict[int, int] = {}
    for seg, counts in votes.items():
        winner, win_count = max(counts.items(), key=lambda kv: kv[1])
        total = sum(counts.values())
        if win_count >= _MIN_SEG_VOTES and win_count / total >= _MIN_VOTE_FRAC:
            seg_anchor[seg] = winner
    return seg_anchor


_CLOCK_TIME_RE = re.compile(
    r"^(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?:\.(\d))?$"
)


def _parse_clock_text(raw: str) -> float | None:
    """Parse OCR'd clock text into total seconds remaining. Handles every
    format confirmed on real video: "M:SS" (e.g. "3:00"), "MM:SS" (e.g.
    "09:50"), "H:MM:SS" for a long time control, and the "M:SS.T"
    tenths-of-a-second display chess.com switches to once a clock drops
    under ~4 seconds (see clock_read.py's module docstring history).

    Deliberately conservative on anything that doesn't match one of these
    shapes (e.g. a bare "555" with no colon at all, which could be a
    genuine MMSS run Tesseract failed to put a colon in, or could just as
    easily be noise) - reporting nothing is better than guessing wrong,
    same philosophy as the rest of this module."""
    raw = raw.strip()
    # A leading icon (a pause/circle glyph some layouts render right before
    # the digits - confirmed on a real video) can OCR as junk characters
    # ahead of the actual time - trim anything before the first digit
    # rather than anchoring the match at the very start of the string.
    first_digit = next((i for i, c in enumerate(raw) if c.isdigit()), None)
    if first_digit is None:
        return None
    raw = raw[first_digit:]
    m = _CLOCK_TIME_RE.match(raw)
    if m:
        hours_s, a, b, tenths_s = m.groups()
        if hours_s is not None:
            hours, minutes, seconds = int(hours_s), int(a), int(b)
        else:
            hours, minutes, seconds = 0, int(a), int(b)
        if minutes >= 60 or seconds >= 60:
            return None
        total = hours * 3600 + minutes * 60 + seconds
        if tenths_s is not None:
            total += int(tenths_s) / 10.0
        return total
    # Fallback: Tesseract occasionally drops the colon entirely (confirmed
    # on a real video: "09:30" -> "0930"), leaving an unambiguous 3-4 bare
    # digits - the same MM:SS width a real clock always renders at. Reject
    # anything else bare (1-2 digits, or 5+) as too ambiguous to guess.
    if raw.isdigit() and 3 <= len(raw) <= 4:
        minutes, seconds = int(raw[:-2]), int(raw[-2:])
        if seconds < 60:
            return minutes * 60 + seconds
    return None


def read_clock_ocr(frames: list[np.ndarray], times: list[float], box: ClockBox) -> list[ClockReading]:
    """Read a single clock's value directly from each frame via Tesseract -
    no self-calibration, no bootstrap wraparound needed (contrast
    read_clock below). Tested head-to-head against read_clock on several
    real games: it has strictly higher coverage (self-calibration needs
    ~10 real ticking seconds before it can read anything at all, Tesseract
    reads the very first frame), and - more importantly - it caught real
    self-calibration failures that read_clock reported with high or even
    100% confidence: a clock self-calibration confidently read as "11:54"
    for an entire game was actually "10:00" throughout (confirmed by eye),
    a wrong anchor picked up early and propagated with full apparent
    confidence for the rest of the game. This isn't a coverage tradeoff -
    it's a strictly better mechanism for this module's job, since it never
    needs a persistent per-video "digit font" model that a bad early guess
    can corrupt for the rest of the video.

    Runs the same outlier-rejection pass as read_clock afterward (a single
    reading inconsistent with both neighbors' trend is more likely a
    misread than a real jump) - Tesseract is far more reliable than the
    old per-glyph classifier, but not perfect, and that check is generic
    over any ClockReading sequence regardless of how it was produced.
    """
    readings = []
    for t, frame in zip(times, frames):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        x0, y0 = max(0, box.x - 3), max(0, box.y - 3)
        crop = gray[y0:box.y + box.h + 3, x0:box.x + box.w + 3]
        seconds = None
        if crop.size > 0:
            big = cv2.resize(crop, None, fx=8, fy=8, interpolation=cv2.INTER_CUBIC)
            data = pytesseract.image_to_data(big, config="--psm 7", output_type=pytesseract.Output.DICT)
            raw = "".join(tok for tok in data["text"] if tok.strip())
            seconds = _parse_clock_text(raw)
        readings.append(ClockReading(t=t, seconds=seconds))
    return _reject_outliers(readings)


def read_clock(frames: list[np.ndarray], times: list[float], box: ClockBox) -> list[ClockReading]:
    """Read a single clock's value (in seconds) across a frame sequence,
    self-calibrating the digit font from the countdown's own behavior.
    Superseded by read_clock_ocr above for actual use (see its docstring) -
    kept for reference/comparison, not called from scan.py anymore.

    frames/times must be time-ordered. Somewhere in the sequence needs at
    least one real 0-9 wraparound of the seconds digit (about ten real
    ticking seconds of *this* clock actually running) to bootstrap templates
    - without that, every reading comes back None. Once templates exist,
    every reliably-spaced run of frames (see _segments) is read independently
    by majority vote and doesn't need its own wraparound.
    """
    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames]

    per_frame_chars: list[list[tuple[int, int, int, int]] | None] = []
    per_frame_mask: list[np.ndarray | None] = []
    for g in grays:
        gcrop = box.crop(g)   # Box.crop only slices on the first two axes, works for grayscale too
        res = _tight_ink_bbox(gcrop)
        if res is None:
            per_frame_chars.append(None)
            per_frame_mask.append(None)
            continue
        mask, bbox = res
        chars = _strip_leading_icon(_strip_decimal_tenths(mask, _segment_chars(mask, bbox)))
        if len(chars) not in (4, 5):   # only M:SS / MM:SS supported for now
            per_frame_chars.append(None)
            per_frame_mask.append(None)
            continue
        per_frame_chars.append(chars)
        per_frame_mask.append(mask)

    valid_idx = [i for i, c in enumerate(per_frame_chars) if c is not None]
    if len(valid_idx) < 4:
        return [ClockReading(t, None) for t in times]

    # The tight-ink vertical bounds can jitter by a pixel between otherwise
    # identical frames (the border-peel is content-sensitive), which alone
    # is enough to throw off glyph-to-glyph comparison after resizing. Since
    # a clock's text height doesn't actually change frame to frame, pin every
    # frame to the consensus (mode) vertical band instead of trusting each
    # frame's own bbox.
    y_pairs = [(per_frame_chars[i][0][2], per_frame_chars[i][0][3]) for i in valid_idx]
    canon_y0, canon_y1 = max(set(y_pairs), key=y_pairs.count)
    for i in valid_idx:
        h = per_frame_mask[i].shape[0]
        y0, y1 = max(0, min(canon_y0, h - 1)), max(0, min(canon_y1, h - 1))
        per_frame_chars[i] = [(x0, x1, y0, y1) for x0, x1, _, _ in per_frame_chars[i]]

    # A frame's ones/tens-of-seconds character can occasionally come out
    # fused with its neighbor (e.g. video-compression blur bridging the gap
    # between two adjacent digits for one frame), which segments as one
    # anomalously wide "character" - roughly 2x a real single digit's width.
    # Real single-digit widths already vary a lot ("1" vs "0"), so filter by
    # comparing against the observed spread rather than a fixed pixel count.
    def _char_w(i, pos):
        c = per_frame_chars[i][pos]
        return c[1] - c[0] + 1

    widths = [_char_w(i, -1) for i in valid_idx] + [_char_w(i, -2) for i in valid_idx]
    max_w = 1.6 * max(float(np.percentile(widths, 90)), 1.0)
    valid_idx = [i for i in valid_idx if _char_w(i, -1) <= max_w and _char_w(i, -2) <= max_w]
    if len(valid_idx) < 4:
        return [ClockReading(t, None) for t in times]

    # The colon can occasionally fail to segment at all (its ink is faint
    # enough some frames that _tight_ink_bbox's threshold misses it), which
    # silently shifts every character after it by one position: "08:58"
    # loses its colon and segments as 4 characters - the same count a real
    # 4-char "M:SS" frame has, but here they're MM:SS's two minute digits
    # plus the two seconds digits, not one minute digit plus a colon. Don't
    # assume which is which by a fixed count; check whether the character
    # 3-from-the-end actually looks like a colon (see _is_colon) and only
    # drop it from the minutes reading if it does.
    per_frame_minute_chars: dict[int, tuple] = {}
    for i in valid_idx:
        chars, mask = per_frame_chars[i], per_frame_mask[i]
        if _is_colon(mask, chars[-3]):
            per_frame_minute_chars[i] = chars[:-3]
        else:
            per_frame_minute_chars[i] = chars[:-2]

    ones_glyphs, tens_glyphs = [], []
    n_minute_slots = max((len(per_frame_minute_chars[i]) for i in valid_idx), default=0)
    minute_slot_glyphs: list[list[np.ndarray | None]] = [[] for _ in range(n_minute_slots)]
    for i in valid_idx:
        chars, mask = per_frame_chars[i], per_frame_mask[i]
        ones_glyphs.append(_char_glyph(mask, chars[-1]))
        tens_glyphs.append(_char_glyph(mask, chars[-2]))
        minute_chars = per_frame_minute_chars[i]   # right-aligned: [-1]=ones-of-minutes, [-2]=tens-of-minutes, ...
        for s in range(n_minute_slots):
            pos = len(minute_chars) - 1 - s
            minute_slot_glyphs[s].append(_char_glyph(mask, minute_chars[pos]) if pos >= 0 else None)

    valid_times = [times[i] for i in valid_idx]
    seg_id = _segments(valid_times)
    ones_thresh = _calibrate_same_sim(ones_glyphs, seg_id)
    tens_thresh = _calibrate_same_sim(tens_glyphs, seg_id)
    ones_boot = _bootstrap_ones_digits(ones_glyphs, tens_glyphs, seg_id, ones_thresh, tens_thresh)
    if not any(v is not None for v in ones_boot):
        return [ClockReading(t, None) for t in times]

    # A segment anchored by only one observed wrap has no way to check
    # itself: if that single wrap was mistimed (same/different misjudged
    # right at that moment), every one of its labels is shifted by a
    # constant with nothing locally inconsistent to catch it (measured on
    # a real video: a whole digit's worth of examples, labeled "1", that
    # were actually all "2"). Two or more wraps at least confirm each other
    # were counted correctly against each other. Templates are shared
    # across every segment in this clock, so a single under-verified
    # segment corrupting one digit's template would silently break reading
    # for every *other* segment too - only let well-confirmed segments
    # vote on what a digit looks like. An under-verified segment can still
    # be read (below) once templates exist from segments that did qualify.
    wrap_anchors = _wrap_anchors(ones_glyphs, tens_glyphs, seg_id, ones_thresh, tens_thresh)
    trusted_segs = {seg for seg, anchors in wrap_anchors.items() if len(anchors) >= 2}
    template_labels = [v if v is not None and seg_id[k] in trusted_segs else None
                        for k, v in enumerate(ones_boot)]

    ones_templates = _build_templates(ones_glyphs, template_labels, ones_thresh)
    if sum(t is not None for t in ones_templates) < 10:
        return [ClockReading(t, None) for t in times]

    # Precise, timing-only tick count since each run began - this doesn't
    # depend on classifying digit shape at all, so it's not vulnerable to the
    # same misreads as the vote below. But used raw, it still lets noise
    # compound over a long segment: a same/different misjudgment anywhere
    # along the way permanently shifts every later frame's tick count by a
    # constant, randomly walking the eventual vote away from the truth a
    # little further the longer the segment runs (confirmed on a real video:
    # a whole segment's majority vote settled 10 seconds off despite every
    # individual digit classification being fine). Every wrap-anchor already
    # re-derives "0" from scratch every ~10 ticks (see
    # _bootstrap_ones_digits) specifically so errors can't compound past one
    # wrap - re-deriving *ticks* the same way corrects for that same noise
    # while still combining every frame in the segment into one vote, rather
    # than splitting into many small, weakly-supported per-wrap votes.
    rel_ticks, _ = _relative_ticks(ones_glyphs, seg_id, ones_thresh)
    _, tens_changed = _relative_ticks(tens_glyphs, seg_id, tens_thresh)
    corrected_ticks = _corrected_ticks(rel_ticks, seg_id, wrap_anchors, tens_changed)

    # Each frame independently guesses what the clock read at its segment's
    # first anchor (its own classified value, walked back by its own
    # corrected elapsed ticks). A correctly-classified frame always lands on
    # the same guess as every other correctly-classified frame in that
    # segment; a misclassified frame almost never lands on that same value
    # by coincidence. So the segment's actual value is just whichever guess
    # wins the most votes - a single bad frame no longer corrupts everything
    # downstream of it (the old failure mode: classify once per transition,
    # trust it until the next one).
    raw_guesses: dict[int, list[int]] = {}   # segment -> [guesses]
    for k, i in enumerate(valid_idx):
        if corrected_ticks[k] is None:
            continue
        n_minutes_here = len(per_frame_minute_chars[i])
        m_digits = [_classify(minute_slot_glyphs[s][k], ones_templates) for s in range(n_minutes_here)]
        tens_d = _classify(tens_glyphs[k], ones_templates, valid_digits=range(6))
        ones_d = ones_boot[k] if ones_boot[k] is not None else _classify(ones_glyphs[k], ones_templates)
        if tens_d is None or ones_d is None or any(d is None for d in m_digits):
            continue
        # m_digits[s] is the slot-s digit (0 = ones-of-minutes, 1 = tens-of-
        # minutes, ...), least-significant first - sum by place value rather
        # than the more error-prone "accumulate left to right" pattern, which
        # would silently reverse a 2+ digit minutes count (e.g. read "10:00"
        # as "1:00" - exactly what a real 2-digit-minutes video hit).
        minutes = sum(d * 10 ** s for s, d in enumerate(m_digits))
        value_now = minutes * 60 + tens_d * 10 + ones_d
        guess = round(value_now + corrected_ticks[k])
        raw_guesses.setdefault(seg_id[k], []).append(guess)

    # A handful of frames land 1-2 seconds off the rest even when everything
    # is basically working (small residual tick-counting noise, distinct
    # from a wrong classification landing tens/minutes away) - group guesses
    # within a few seconds of each other before counting, so that noise
    # doesn't fragment an otherwise-clear majority into several near-miss
    # non-majorities.
    votes = {seg: _cluster_votes(g) for seg, g in raw_guesses.items()}
    seg_anchor = _vote_for_anchors(votes)

    readings: list[ClockReading] = [ClockReading(t, None) for t in times]
    for k, i in enumerate(valid_idx):
        if corrected_ticks[k] is None:
            continue
        anchor = seg_anchor.get(seg_id[k])
        if anchor is None:
            continue
        readings[i] = ClockReading(times[i], float(round(anchor - corrected_ticks[k])))

    return _reject_outliers(readings)


def _corrected_ticks(rel_ticks: list[int], seg_id: list[int],
                      wrap_anchors: dict[int, list[tuple[int, int]]],
                      tens_changed: list[bool]) -> list[float | None]:
    """Re-express each frame's elapsed ticks relative to its segment's first
    anchor, but with every anchor-to-anchor gap replaced by however many
    real wraps actually separate them - what it must actually be - rather
    than trusting the raw (possibly noisy) tick count between them. A frame
    with no anchor in its segment at all can't be corrected this way and
    gets None.

    The number of real wraps between two anchors is counted directly from
    tens_changed events in that span, not inferred by rounding the raw tick
    gap to the nearest multiple of 10: a dropped-frames hiccup in the
    source video can make the ones digit skip several values in a single
    sampled step (confirmed on a real video: on-screen "09:41" one sampled
    frame, "09:36" the next), which under-counts the raw tick gap enough
    that rounding it picks the wrong multiple entirely - a true 20-tick gap
    measured as 14 raw ticks rounds to 10, not 20. The tens digit's own
    change events aren't fooled by this the same way: a real wrap flips it
    exactly once no matter how many ones-ticks got collapsed into that
    step, so counting those events directly gets the true wrap count right
    even when the ones-tick gap alone can't say which multiple it should be.

    Between two anchors, the correction is applied by *scaling* the raw
    tick count to fit the corrected gap, not by snapping to whichever
    anchor is nearest and adding a lump correction there: the latter is
    exact at both anchors too, but creates an artificial jump right at
    the midpoint between them when the raw gap wasn't already a clean
    multiple of 10 - confirmed on a real video, as a handful of readings
    2-3 seconds off *only* in a narrow band around each such midpoint,
    despite the anchors on both sides being individually correct. Scaling
    distributes that same correction smoothly across the whole span
    instead, since a real countdown ticks at a constant rate through it.
    """
    corrected_pos_by_seg: dict[int, dict[int, int]] = {}
    for seg, anchors in wrap_anchors.items():
        ordered = sorted(anchors)
        pos = {ordered[0][0]: 0}
        for (prev_tick, prev_idx), (cur_tick, cur_idx) in zip(ordered, ordered[1:]):
            n_wraps = max(1, sum(1 for c in tens_changed[prev_idx + 1:cur_idx + 1] if c))
            pos[cur_tick] = pos[prev_tick] + 10 * n_wraps
        corrected_pos_by_seg[seg] = pos

    offsets: list[float | None] = [None] * len(rel_ticks)
    for k in range(len(rel_ticks)):
        anchors = wrap_anchors.get(seg_id[k])
        if not anchors:
            # No wrap ever confirmed itself in this segment - too short, or
            # never ticked - so there's nothing to correct raw ticks
            # against. Falling back to the raw (uncorrected) count still
            # lets a short segment be read via templates already trusted
            # from elsewhere in the clock; drift only becomes a problem
            # over the many-tick runs a correction is actually available
            # for.
            offsets[k] = float(rel_ticks[k])
            continue
        ordered = sorted(t for t, _ in anchors)
        pos = corrected_pos_by_seg[seg_id[k]]
        t = rel_ticks[k]
        if t <= ordered[0] or t >= ordered[-1]:
            a = ordered[0] if t <= ordered[0] else ordered[-1]
            offsets[k] = pos[a] + (t - a)
            continue
        a = max(x for x in ordered if x <= t)
        b = min(x for x in ordered if x >= t)
        if a == b:
            offsets[k] = pos[a]
        else:
            frac = (t - a) / (b - a)
            offsets[k] = pos[a] + frac * (pos[b] - pos[a])
    return offsets


def _reject_outliers(readings: list[ClockReading], tol: float = 3.0) -> list[ClockReading]:
    """Drop individual readings that don't fit either neighbor's trend.

    A ticking clock decreases by ~1 second per real second (with occasional
    flat stretches when paused, or a small jump up on a move that gains
    increment) - so a reading should be predictable from a nearby valid
    neighbor once the elapsed time between them is accounted for. One that
    matches *neither* the previous nor the next valid reading is almost
    always a single-frame misread, not a real value - better to report
    nothing for that instant than something wrong.
    """
    idx = [i for i, r in enumerate(readings) if r.seconds is not None]
    out = list(readings)
    for pos, i in enumerate(idx):
        v, t = readings[i].seconds, readings[i].t
        ok_prev = pos == 0
        if pos > 0:
            pv, pt = readings[idx[pos - 1]].seconds, readings[idx[pos - 1]].t
            ok_prev = abs(v - (pv - (t - pt))) <= tol
        ok_next = pos == len(idx) - 1
        if pos < len(idx) - 1:
            nv, nt = readings[idx[pos + 1]].seconds, readings[idx[pos + 1]].t
            ok_next = abs(v - (nv + (nt - t))) <= tol
        if not ok_prev and not ok_next:
            out[i] = ClockReading(t, None)
    return out


def side_for_box(clock_box: ClockBox, board_box, flipped: bool) -> str:
    """Which player's clock this is, given the board's own orientation.

    Broadcast layouts consistently show each player's own info/clock on the
    same side of the screen as their own pieces on the board (opponent on
    top, yourself on the bottom) - so a clock above the board's vertical
    center belongs to whoever's pieces are on top, and a clock below it
    belongs to whoever's on the bottom. `flipped` (already known from board
    orientation - see reconstruct.py) says which color that bottom side is.
    """
    bx, by, bw, bh = board_box.as_tuple() if hasattr(board_box, "as_tuple") else board_box
    top = clock_box.y < by + bh / 2
    bottom_side = "black" if flipped else "white"
    top_side = "white" if flipped else "black"
    return top_side if top else bottom_side


def clock_at_times(readings: list[ClockReading], query_times: list[float]) -> list[float | None]:
    """The clock's value at each of `query_times`, taken from the most
    recent sampled reading at or before it - a clock only ever moves
    forward between samples, so the last known value is the best available
    answer for "what did it read at this instant". `query_times` must be
    sorted ascending (true of a game's own move_seconds, which this is
    built to be queried with)."""
    ordered = sorted((r.t, r.seconds) for r in readings if r.seconds is not None)
    out: list[float | None] = []
    j = 0
    last: float | None = None
    for qt in query_times:
        while j < len(ordered) and ordered[j][0] <= qt:
            last = ordered[j][1]
            j += 1
        out.append(last)
    return out
