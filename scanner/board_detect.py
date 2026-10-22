"""Locate an axis-aligned digital chessboard inside a video frame.

Digital boards (lichess, chess.com, GUIs) are shown head-on, so the board is an
axis-aligned rectangle (near-square) holding a clean 8x8 checkerboard. Two
stages:

1. Coarse search over (x, y, size) scored by how well an 8x8
   alternating-brightness model fits the centre of each cell.
2. Grid refinement: within an expanded ROI, fit a uniform 9-line grid to the
   image's gradient energy on each axis independently, snapping the box to the
   real board lines (sub-cell accuracy, tolerates a slightly non-square crop).
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

_PARITY = np.indices((8, 8)).sum(axis=0) % 2
_IDEAL = np.where(_PARITY == 0, 1.0, -1.0)


@dataclass
class Box:
    x: int
    y: int
    w: int
    h: int
    score: float = 0.0

    @property
    def size(self) -> int:
        return (self.w + self.h) // 2

    def as_tuple(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.w, self.h


def _cell_stats(ii: np.ndarray, ii2: np.ndarray, xs: np.ndarray, ys: np.ndarray):
    x0 = np.round(xs[:-1] + (xs[1:] - xs[:-1]) * 0.25).astype(np.intp)
    x1 = np.round(xs[:-1] + (xs[1:] - xs[:-1]) * 0.75).astype(np.intp)
    y0 = np.round(ys[:-1] + (ys[1:] - ys[:-1]) * 0.25).astype(np.intp)
    y1 = np.round(ys[:-1] + (ys[1:] - ys[:-1]) * 0.75).astype(np.intp)
    X0, Y0 = np.meshgrid(x0, y0)
    X1, Y1 = np.meshgrid(x1, y1)
    area = np.maximum((X1 - X0) * (Y1 - Y0), 1)

    def rs(I):
        return I[Y1, X1] - I[Y0, X1] - I[Y1, X0] + I[Y0, X0]

    mean = rs(ii) / area
    var = np.maximum(rs(ii2) / area - mean * mean, 0.0)
    return mean, var


def _score(mean: np.ndarray, var: np.ndarray) -> float:
    m = mean - mean.mean()
    denom = np.sqrt((m * m).mean()) + 1e-6
    corr = float((m * _IDEAL).mean() / denom)
    a = mean[_PARITY == 0].mean()
    b = mean[_PARITY == 1].mean()
    spread = mean.max() - mean.min() + 1e-6
    separation = abs(a - b) / spread
    noise = np.sqrt(var).mean() / spread
    return abs(corr) * separation - 0.5 * noise


def _coarse(gray: np.ndarray) -> Box:
    h, w = gray.shape
    ii, ii2 = cv2.integral2(gray.astype(np.float64))
    best = Box(0, 0, 0, 0, -1e9)
    step = max(4, int(0.025 * h))
    for size in range(int(0.32 * h), int(min(h, w) * 0.99) + 1, max(step, int(0.035 * h))):
        for y in range(0, h - size + 1, step):
            ys = np.linspace(y, y + size, 9)
            for x in range(0, w - size + 1, step):
                xs = np.linspace(x, x + size, 9)
                sc = _score(*_cell_stats(ii, ii2, xs, ys))
                if sc > best.score:
                    best = Box(x, y, size, size, sc)
    return best


def _fit_lines(profile: np.ndarray, lo: int, hi: int, pitch_lo: float, pitch_hi: float,
               pitch: float | None = None):
    """Best (start, pitch, confidence) so that start + i*pitch (i=0..8) sits on
    profile peaks. If *pitch* is given, only the start position is searched -
    used to re-fit one axis to a pitch already trusted from the other (see
    _refine_grid: a chessboard is always square, so an independent fit on each
    axis can disagree only because one of them is wrong)."""
    prof = profile.astype(np.float64)
    prof = prof / (prof.max() + 1e-9)
    n = len(prof)
    prof = np.concatenate([prof, np.zeros(int(pitch_hi * 8) + 2)])  # off-board -> no energy
    lo = max(0, min(lo, n - 1))
    hi = max(lo, min(hi, n - 1))
    pitches = [pitch] if pitch is not None else np.arange(pitch_lo, pitch_hi, 0.5)
    best_val, best = -1.0, (lo, pitch if pitch is not None else (pitch_lo + pitch_hi) / 2)
    for p in pitches:
        for start in range(lo, hi + 1):
            idx = np.round(start + np.arange(9) * p).astype(int)
            val = prof[idx].sum()
            if val > best_val:
                best_val, best = val, (start, p)
    return best[0], best[1], best_val


def _refine_grid(gray: np.ndarray, box: Box) -> Box:
    h, w = gray.shape
    pad = int(0.14 * box.size)
    x0 = max(0, box.x - pad)
    y0 = max(0, box.y - pad)
    x1 = min(w, box.x + box.w + pad)
    y1 = min(h, box.y + box.h + pad)
    roi = gray[y0:y1, x0:x1].astype(np.float32)
    if roi.size == 0:
        return box

    gx = np.abs(cv2.Sobel(roi, cv2.CV_32F, 1, 0, ksize=3)).sum(axis=0)
    gy = np.abs(cv2.Sobel(roi, cv2.CV_32F, 0, 1, ksize=3)).sum(axis=1)

    base = box.size / 8.0
    lo_p, hi_p = base * 0.8, base * 1.25
    x_lo, x_hi = max(0, box.x - x0 - pad // 2), box.x - x0 + pad // 2
    y_lo, y_hi = max(0, box.y - y0 - pad // 2), box.y - y0 + pad // 2
    sx, px, vx = _fit_lines(gx, x_lo, x_hi, lo_p, hi_p)
    sy, py, vy = _fit_lines(gy, y_lo, y_hi, lo_p, hi_p)

    # a chessboard is always square - if the two axes disagree on pitch, one
    # of them fit a false pattern (a UI panel's edge, board coordinates/text
    # cutting into the sampled region). Trust whichever axis fit more
    # strongly and re-fit the other axis's *start* to that same pitch, rather
    # than returning a box that's provably wrong before a single cell is ever
    # read from it.
    if abs(px - py) > 0.5:
        if vx >= vy:
            sy, py, _ = _fit_lines(gy, y_lo, y_hi, lo_p, hi_p, pitch=px)
        else:
            sx, px, _ = _fit_lines(gx, x_lo, x_hi, lo_p, hi_p, pitch=py)

    return Box(
        x=int(round(x0 + sx)),
        y=int(round(y0 + sy)),
        w=int(round(px * 8)),
        h=int(round(py * 8)),
        score=box.score,
    )


EDGE_PAD = 0.04   # share of the frame height the board may run past each edge


def detect_in_frame(frame_bgr: np.ndarray, work_height: int = 480) -> Box:
    h0, w0 = frame_bgr.shape[:2]
    scale = work_height / h0
    small = cv2.resize(frame_bgr, (int(w0 * scale), work_height), interpolation=cv2.INTER_AREA)
    gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (3, 3), 0)

    box = _refine_grid(gray, _coarse(gray))
    # The board may be a little bigger than the frame (Naroditsky's Sensei
    # speedruns: the top and bottom ranks and the h-file clipped by a few
    # pixels). Searching only boxes inside the frame put it 24 px low, or
    # squashed it (723x717), and no opening position was ever read. Where
    # the box touches the frame's edge or isn't square, search again with
    # the frame padded by its own edge pixels (they carry the clipped
    # squares' colours on), letting the box run past the edge, and keep the
    # better fit. Boards well inside the frame are left as they were.
    pad = int(round(EDGE_PAD * work_height))
    gh, gw = gray.shape
    edge = 3
    if pad and (box.x <= edge or box.y <= edge or box.x + box.w >= gw - edge or box.y + box.h >= gh - edge
                or abs(box.w - box.h) > 2):
        padded = cv2.copyMakeBorder(gray, pad, pad, pad, pad, cv2.BORDER_REPLICATE)
        alt = _refine_grid(padded, _coarse(padded))
        if alt.score > box.score:
            box = Box(alt.x - pad, alt.y - pad, alt.w, alt.h, alt.score)
    inv = 1.0 / scale
    return Box(int(round(box.x * inv)), int(round(box.y * inv)),
              int(round(box.w * inv)), int(round(box.h * inv)), box.score)


def detect(frames: list[np.ndarray], min_score: float = 0.03, cluster_tol: int = 20) -> Box | None:
    """Detect the board box from several sample frames.

    Some board themes (e.g. flat, low-contrast grey/white squares) score below
    any reasonable absolute threshold even when correctly located - filtering
    hits by score first can then discard every correct detection and leave
    only a rare, higher-scoring false positive elsewhere in the frame (a UI
    panel with sharper contrast). The board itself doesn't move between
    samples, so instead of trusting the highest score, group hits that
    roughly agree with each other and trust whichever group has the most
    votes; `min_score` is now just a floor to reject a frame set with no
    plausible board at all, not the primary filter.
    """
    hits = [detect_in_frame(f) for f in frames]
    hits = [b for b in hits if b.score >= min_score]
    if not hits:
        return None

    clusters: list[list[Box]] = []
    for b in hits:
        for cluster in clusters:
            r = cluster[0]
            if (abs(b.x - r.x) <= cluster_tol and abs(b.y - r.y) <= cluster_tol
                    and abs(b.w - r.w) <= cluster_tol and abs(b.h - r.h) <= cluster_tol):
                cluster.append(b)
                break
        else:
            clusters.append([b])
    best = max(clusters, key=len)

    med = lambda vs: int(np.median(vs))  # noqa: E731
    return Box(
        med([b.x for b in best]), med([b.y for b in best]),
        med([b.w for b in best]), med([b.h for b in best]),
        float(np.mean([b.score for b in best])),
    )


def draw(frame_bgr: np.ndarray, box: Box) -> np.ndarray:
    out = frame_bgr.copy()
    x, y, w, h = box.as_tuple()
    cv2.rectangle(out, (x, y), (x + w, y + h), (0, 0, 255), 2)
    for i in range(1, 8):
        gx = x + round(w * i / 8)
        gy = y + round(h * i / 8)
        cv2.line(out, (gx, y), (gx, y + h), (0, 255, 0), 1)
        cv2.line(out, (x, gy), (x + w, gy), (0, 255, 0), 1)
    cv2.putText(out, f"score={box.score:.3f} {w}x{h}", (x, max(20, y - 8)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
    return out
