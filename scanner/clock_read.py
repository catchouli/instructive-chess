"""Read the two on-screen clocks (one per player) from a video, with zero
external assets - same self-calibrating philosophy as piece recognition in
recognize.py, just applied to clock digits instead of piece icons.

Clock position varies wildly across sites/layouts (anchored to the board's
corners on some broadcasts, in a dedicated panel far from the board on
others) so it can't be hardcoded. Instead we *locate* the two clocks from how
they behave: a countdown clock is one of the only things in the frame that
changes in a small, fixed-position, text-shaped region over time (unlike a
moving face, a scrolling chat, or a static logo/name).

Reading the digits without any font/template assets works because a
countdown clock's fastest-changing digit (ones of seconds) cycles through
0-9 in a fixed, known, descending order once every ten real seconds. Rather
than clustering glyph images into 10 shape-based buckets up front - which
turned out to be fragile, since real per-frame renders of the very same
digit are noisy enough (compression, sub-pixel jitter) to fragment or blend
together - the value is derived from *timing* first: counting "did this
position's glyph change from the previous frame" gives a running tick
count, and the one frame where the ones digit wraps 9->8->...->0 exactly
overlaps a tens-of-seconds change, anchoring that tick count to an absolute
value with no external reference needed. Only after that gives us a small
set of confidently-labeled example glyphs do we fall back to ordinary
nearest-template shape matching - to fill any frame timing couldn't reach,
and to read the tens-of-seconds and minutes positions (which share the same
font/size but don't tick predictably enough to bootstrap on their own).

This is good enough to track a clock's trend reliably, but not yet perfect:
occasional frames (particularly on unfamiliar fonts, or right after a video
compression artifact) still get a single position misclassified for a few
frames before the next real change self-corrects it. `_reject_outliers`
catches single-frame spikes; short self-consistent runs of a wrong value
can still slip through. Treat individual readings as best-effort, not
ground truth.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

_CHANGE_LO = 0.10   # a ticking clock changes less than 100% of frame-steps
_CHANGE_HI = 0.85   # (only one side's clock runs at a time) but more than ~0
_DIFF_THRESH = 20   # per-pixel gray delta counted as "changed"

_GLYPH_H = 16            # every digit glyph is rescaled to this height for comparison
_GLYPH_W = 14            # then padded/cropped to this width, preserving aspect ratio -
                         # a thin "1" and a wide "0" must stay visually distinct, so
                         # width is never stretched to fit like height is
_SAME_SIM = 0.80         # ZNCC above this = "still showing the same digit"


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
        if area < 60 or w < 25 or h < 10 or h > 60:
            continue
        aspect = w / max(h, 1)
        if not (1.2 <= aspect <= 10.0):
            continue
        # Validate by segmentation, not just size/aspect: a webcam or other
        # moving UI clutter can easily match the size/shape filters above by
        # coincidence, but won't cleanly segment into a plausible clock
        # character count the way real "M:SS"/"MM:SS"/"H:MM:SS" text does.
        px0, py0 = max(0, x - pad), max(0, y - pad)
        px1, py1 = min(w_img, x + w + pad), min(h_img, y + h + pad)
        crop = ref[py0:py1, px0:px1]
        res = _tight_ink_bbox(crop)
        if res is None:
            continue
        mask, bbox = res
        n_chars = len(_segment_chars(mask, bbox))
        if n_chars not in (4, 5, 7):
            continue
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

    Height is normalized by resizing (it's ~constant for a given clock
    anyway), but width is only ever scaled by that same factor and then
    padded/cropped, never stretched independently - a narrow "1" and a wide
    "0" need to stay visually distinct, not be squashed to the same shape.
    """
    x0, x1, y0, y1 = char
    patch = (mask[y0:y1 + 1, x0:x1 + 1].astype(np.uint8)) * 255
    h, w = patch.shape
    scale = _GLYPH_H / max(h, 1)
    new_w = max(1, min(round(w * scale), _GLYPH_W))
    resized = cv2.resize(patch, (new_w, _GLYPH_H), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((_GLYPH_H, _GLYPH_W), np.uint8)
    x_off = (_GLYPH_W - new_w) // 2
    canvas[:, x_off:x_off + new_w] = resized
    return canvas


def _similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Zero-normalized cross-correlation between two glyph images.

    Raw pixel agreement is a bad metric here: a 16x14 glyph canvas is mostly
    black background, so any two glyphs already agree on ~80% of pixels
    before the actual digit shape is considered at all, making genuinely
    different digits look deceptively "similar". Mean-centering (as
    recognize.py already does for piece templates) cancels that shared
    background out and compares the actual ink pattern instead.
    """
    af, bf = a.astype(np.float32), b.astype(np.float32)
    af, bf = af - af.mean(), bf - bf.mean()
    num = float((af * bf).sum())
    den = float(np.sqrt((af * af).sum() * (bf * bf).sum()))
    return num / den if den > 1e-6 else 0.0


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


def _track_slot(glyphs: list[np.ndarray | None], seg_id: list[int],
                 templates: list[np.ndarray | None]) -> list[int | None]:
    """Read one digit position frame-by-frame, but only ever re-classify it
    against the template set when the glyph actually changed (or a timing
    gap, or the slot disappearing for a frame, broke continuity) - otherwise
    just carry the previous value forward.

    A digit like the minutes column barely ever changes, so classifying it
    independently on every single frame just multiplies the chances of one
    noisy frame getting misread; trusting "still looks the same" instead
    means a digit only has to be classified correctly once per real change.
    A glyph of None means this position doesn't exist in that frame (e.g. a
    tens-of-minutes digit before the count reaches 10).
    """
    values: list[int | None] = [None] * len(glyphs)
    prev_glyph = None
    prev_seg = None
    prev_val = None
    for i, (g, seg) in enumerate(zip(glyphs, seg_id)):
        if g is None:
            prev_glyph = None
            continue
        if seg != prev_seg or prev_glyph is None or _similarity(g, prev_glyph) < _SAME_SIM:
            prev_val = _classify(g, templates)
        values[i] = prev_val
        prev_glyph, prev_seg = g, seg
    return values


def _bootstrap_ones_digits(ones_glyphs: list[np.ndarray], tens_glyphs: list[np.ndarray],
                            times: list[float]) -> list[int | None]:
    """Derive an absolute digit value (0-9) for as many ones-of-seconds
    frames as possible from pure timing, without classifying glyph shape at
    all: a countdown's ones digit decrements by exactly one every real
    second, wrapping 9->8->...->0->9 forever, and the tens-of-seconds digit
    only ever changes at the exact instant of that wrap. So within any
    contiguous run of reliably-spaced frames, counting "did the glyph change
    from the previous frame" gives a running tick count, and the one frame
    where both ones and tens changed together anchors that whole run's tick
    count to an absolute value ("0" the moment before the wrap).

    This sidesteps clustering glyphs into 10 shape-based buckets up front,
    which is fragile: real per-frame renders of the *same* digit are noisy
    enough (compression, sub-pixel jitter) that naive clustering fragments
    single digits into several buckets or blends different digits together.
    """
    n = len(ones_glyphs)
    seg_id = _segments(times)

    ticks = [0] * n   # cumulative ticks-since-segment-start
    ones_changed = [False] * n
    tens_changed = [False] * n
    for i in range(1, n):
        reliable = seg_id[i] == seg_id[i - 1]
        o_diff = _similarity(ones_glyphs[i], ones_glyphs[i - 1]) < _SAME_SIM
        t_diff = _similarity(tens_glyphs[i], tens_glyphs[i - 1]) < _SAME_SIM
        ones_changed[i] = reliable and o_diff
        tens_changed[i] = reliable and t_diff
        ticks[i] = (ticks[i - 1] + (1 if o_diff else 0)) if reliable else 0

    # anchor: the frame right before a combined ones+tens change is "0"
    anchors: dict[int, tuple[int, int]] = {}   # segment -> (ticks_at_anchor, value=0)
    for i in range(1, n):
        if ones_changed[i] and tens_changed[i] and seg_id[i - 1] not in anchors:
            anchors[seg_id[i - 1]] = (ticks[i - 1], 0)

    values: list[int | None] = [None] * n
    for i in range(n):
        anchor = anchors.get(seg_id[i])
        if anchor is None:
            continue
        anchor_ticks, anchor_val = anchor
        values[i] = (anchor_val - (ticks[i] - anchor_ticks)) % 10
    return values


def _build_templates(glyphs: list[np.ndarray], values: list[int | None]) -> list[np.ndarray | None]:
    """Pick one clean, confidently-known glyph image per digit 0-9, to reuse
    (via nearest-match) for every other position on the same clock."""
    templates: list[np.ndarray | None] = [None] * 10
    for g, v in zip(glyphs, values):
        if v is not None and templates[v] is None:
            templates[v] = g
    return templates


def _classify(glyph: np.ndarray, templates: list[np.ndarray | None]) -> int | None:
    best_d, best_sim = None, -1.0
    for d, t in enumerate(templates):
        if t is None:
            continue
        sim = _similarity(glyph, t)
        if sim > best_sim:
            best_sim, best_d = sim, d
    return best_d


def read_clock(frames: list[np.ndarray], times: list[float], box: ClockBox) -> list[ClockReading]:
    """Read a single clock's value (in seconds) across a frame sequence,
    self-calibrating the digit font from the countdown's own behavior.

    frames/times must be time-ordered and cover at least one real 0-9
    wraparound of the seconds digit (about ten real ticking seconds) or
    digit values can't be anchored and every reading comes back None.
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
        chars = _segment_chars(mask, bbox)
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

    ones_glyphs, tens_glyphs = [], []
    n_minute_slots = max(len(per_frame_chars[i]) - 3 for i in valid_idx)
    minute_slot_glyphs: list[list[np.ndarray | None]] = [[] for _ in range(n_minute_slots)]
    for i in valid_idx:
        chars, mask = per_frame_chars[i], per_frame_mask[i]
        ones_glyphs.append(_char_glyph(mask, chars[-1]))
        tens_glyphs.append(_char_glyph(mask, chars[-2]))
        minute_chars = chars[:-3]   # right-aligned: [-1]=ones-of-minutes, [-2]=tens-of-minutes, ...
        for s in range(n_minute_slots):
            pos = len(minute_chars) - 1 - s
            minute_slot_glyphs[s].append(_char_glyph(mask, minute_chars[pos]) if pos >= 0 else None)

    valid_times = [times[i] for i in valid_idx]
    seg_id = _segments(valid_times)
    ones_values = _bootstrap_ones_digits(ones_glyphs, tens_glyphs, valid_times)
    if not any(v is not None for v in ones_values):
        return [ClockReading(t, None) for t in times]

    ones_templates = _build_templates(ones_glyphs, ones_values)
    if sum(t is not None for t in ones_templates) < 10:
        return [ClockReading(t, None) for t in times]

    # fill in any frames the timing bootstrap couldn't reach (same
    # change-aware tracking as every other position, only used where the
    # more-reliable timing-derived value is missing), then reuse the same
    # clean templates - learned once, from the ones-of-seconds position - for
    # every other digit position on this clock
    tracked_ones = _track_slot(ones_glyphs, seg_id, ones_templates)
    ones_values = [v if v is not None else tracked_ones[k] for k, v in enumerate(ones_values)]
    tens_values = _track_slot(tens_glyphs, seg_id, ones_templates)
    minute_values = [_track_slot(g, seg_id, ones_templates) for g in minute_slot_glyphs]

    readings: list[ClockReading] = [ClockReading(t, None) for t in times]
    for k, i in enumerate(valid_idx):
        ones_d, tens_d = ones_values[k], tens_values[k]
        m_digits = [mv[k] for mv in minute_values[:len(per_frame_chars[i]) - 3]]
        if ones_d is None or tens_d is None or any(d is None for d in m_digits):
            continue
        minutes = 0
        for d in m_digits:
            minutes = minutes * 10 + d
        readings[i] = ClockReading(times[i], float(minutes * 60 + tens_d * 10 + ones_d))

    return _reject_outliers(readings)


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
