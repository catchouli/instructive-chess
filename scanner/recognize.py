"""Read the piece placement off a detected board region.

Instructive games always begin from the standard position, so instead of
relying on generic sprites we *self-calibrate*: when a frame's occupancy matches
the opening setup we crop the 32 pieces straight out of that frame and use them
as templates for the rest of the video. Same renderer, same theme, same scale
- later matching is then near-exact. Matching uses zero-normalised cross
correlation over the template's piece mask, so square colour and lichess
last-move / check highlights don't matter.

Placement strings are 64 chars in FEN reading order (rank 8 first, file a first)
as seen on screen; a caller that finds the board is flipped just reverses them.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

_PATCH = 80
_MAX_EXEMPLARS = 3
_CLAHE = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(4, 4))   # rescue low-contrast
                                                              # (white piece on a
                                                              # near-white square)
_INSET = 0.08          # trim each cell edge to dodge gridlines, neighbour bleed and
                       # lichess's in-square coordinate glyphs
_MIN_PIECE_FRAC = 0.10  # a real piece always leaves a detectable blob at least
                       # this big (observed real pawns, the smallest piece:
                       # 0.24-0.51; a genuine background/noise blob that still
                       # scored a confident wrong match was observed at 0.057,
                       # so this is set with a wide margin below the smallest
                       # real piece and well above the largest observed noise
                       # blob) - same bar _build_model uses to accept a
                       # template; below it there is nothing to match, so
                       # don't try

# standard start, FEN reading order, white at bottom
START_PLACEMENT = "rnbqkbnr" + "p" * 8 + "." * 32 + "P" * 8 + "RNBQKBNR"


@dataclass
class Reading:
    placement: str
    confidence: float
    n_pieces: int
    occupancy: np.ndarray = field(default_factory=lambda: np.zeros((8, 8), bool))


# --------------------------------------------------------------------------- #
# cell helpers
# --------------------------------------------------------------------------- #

def _cells(frame_bgr: np.ndarray, box):
    """Yield (rank_row, file_col, cell_bgr) with a small inset applied."""
    x, y, w, h = box.as_tuple() if hasattr(box, "as_tuple") else box
    xe = np.linspace(x, x + w, 9)
    ye = np.linspace(y, y + h, 9)
    ix = (w / 8) * _INSET
    iy = (h / 8) * _INSET
    for r in range(8):
        for c in range(8):
            y0, y1 = int(ye[r] + iy), int(ye[r + 1] - iy)
            x0, x1 = int(xe[c] + ix), int(xe[c + 1] - ix)
            yield r, c, frame_bgr[y0:y1, x0:x1]


def _patch(cell_bgr: np.ndarray) -> np.ndarray:
    """Grey _PATCH x _PATCH crop with local contrast stretched (float32)."""
    g = cv2.cvtColor(cell_bgr, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, (_PATCH, _PATCH), interpolation=cv2.INTER_AREA)
    return _CLAHE.apply(g).astype(np.float32)


def _piece_mask(patch: np.ndarray):
    """(mask float32, fg fraction) for a prepared patch.

    A piece is picked up two ways so low-contrast sets work too: pixels that
    differ from the square background, OR pixels enclosed by the piece's edges
    (a light piece on a light square is basically just its outline). Only
    sizeable connected blobs are kept, so board texture and in-square coordinate
    glyphs don't count.
    """
    ring = max(2, int(round(_PATCH * 0.09)))
    border = np.concatenate([
        patch[:ring].ravel(), patch[-ring:].ravel(),
        patch[:, :ring].ravel(), patch[:, -ring:].ravel(),
    ])
    bg = float(np.median(border))
    spread = float(np.median(np.abs(border - bg))) + 3.0
    bright = np.abs(patch - bg) > max(14.0, 3.0 * spread)

    edge = np.abs(cv2.Laplacian(cv2.GaussianBlur(patch, (3, 3), 0), cv2.CV_32F))
    e_border = np.concatenate([
        edge[:ring].ravel(), edge[-ring:].ravel(),
        edge[:, :ring].ravel(), edge[:, -ring:].ravel(),
    ])
    strong = edge > max(6.0, 5.0 * (np.median(e_border) + 1.0))
    strong = cv2.morphologyEx(strong.astype(np.uint8), cv2.MORPH_CLOSE,
                              np.ones((7, 7), np.uint8))

    raw = (bright | (strong > 0)).astype(np.uint8)
    raw = cv2.morphologyEx(raw, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

    num, labels, stats, _ = cv2.connectedComponentsWithStats(raw, connectivity=8)
    big = [k for k in range(1, num) if stats[k, cv2.CC_STAT_AREA] >= 0.03 * raw.size]
    mask = np.isin(labels, big).astype(np.float32) if big else np.zeros_like(raw, np.float32)
    return mask, float(mask.mean())


def _prep(cell_bgr: np.ndarray):
    patch = _patch(cell_bgr)
    mask, frac = _piece_mask(patch)
    return patch, mask, frac


def _zncc_many(patch: np.ndarray, temps: np.ndarray, masks: np.ndarray) -> np.ndarray:
    """ZNCC of one patch against a stack of templates over each template's mask."""
    w = masks.reshape(len(masks), -1).sum(axis=1)
    w = np.maximum(w, 1.0)
    pm = (patch * masks).reshape(len(masks), -1).sum(axis=1) / w
    tm = (temps * masks).reshape(len(masks), -1).sum(axis=1) / w
    dp = (patch[None] - pm[:, None, None]) * masks
    dt = (temps - tm[:, None, None]) * masks
    num = (dp * dt).reshape(len(masks), -1).sum(axis=1)
    den = np.sqrt((dp * dp).reshape(len(masks), -1).sum(axis=1)
                  * (dt * dt).reshape(len(masks), -1).sum(axis=1))
    return np.where(den > 1e-6, num / den, 0.0)


# --------------------------------------------------------------------------- #
# self-calibrated model
# --------------------------------------------------------------------------- #

@dataclass
class BoardModel:
    temps: np.ndarray            # (K, _PATCH, _PATCH) float32
    masks: np.ndarray            # (K, _PATCH, _PATCH) float32
    piece_of: list[str]          # length K, FEN char per template
    flipped: bool
    empty: np.ndarray = None     # (2, _PATCH, _PATCH) mean empty light/dark square
    occ_thresh: float = 0.05     # fallback foreground fraction
    occ_diff: float = 3.5        # mean abs difference from the empty template
    match_thresh: float = 0.42


def vote_stream(readings: list[tuple[float, str | None]],
                radius: int = 2, min_support: int = 3) -> list[tuple[float, str | None]]:
    """Per-square temporal majority vote to squash single-frame recognition flicker."""
    arrs = [np.frombuffer(p.encode(), np.uint8) if p else None for _, p in readings]
    out: list[tuple[float, str | None]] = []
    for i, (ts, _) in enumerate(readings):
        win = [a for a in arrs[max(0, i - radius): i + radius + 1] if a is not None]
        if len(win) < min_support:
            out.append((ts, None))
            continue
        stack = np.stack(win)
        voted = np.array([np.bincount(stack[:, c]).argmax() for c in range(stack.shape[1])],
                         dtype=np.uint8)
        out.append((ts, voted.tobytes().decode()))
    return out


def occupancy(frame_bgr: np.ndarray, box, thresh: float = 0.045) -> np.ndarray:
    occ = np.zeros((8, 8), bool)
    for r, c, cell in _cells(frame_bgr, box):
        if cell.size:
            _, _, frac = _prep(cell)
            occ[r, c] = frac >= thresh
    return occ


def looks_like_start(occ: np.ndarray) -> bool:
    full = occ[[0, 1, 6, 7]].sum()
    empty = occ[2:6].sum()
    # empty<=3 was too strict for some themes: static board texture/coordinate
    # glyphs in the middle ranks can trip this crude pre-filter's occupancy
    # check on an otherwise genuine opening position (observed up to ~9 on a
    # real video). This is only a cheap filter to decide whether a full
    # calibration attempt is worth trying at all - calibrate_from_start's own
    # tolerance check is the real gate, so erring generous here just costs a
    # few wasted calibration attempts, not accuracy.
    return full >= 29 and empty <= 10


def _piece_is_light(cell_bgr: np.ndarray) -> float:
    patch, mask, _ = _prep(cell_bgr)
    if mask.sum() < 20:
        return 0.0
    return float((patch * mask).sum() / mask.sum())


def _align_cost(frame_bgr: np.ndarray, box) -> float:
    """How badly a candidate box fits an opening position: pieces should be
    centred in the outer two ranks and the middle four ranks should be blank."""
    cost = 0.0
    for r, c, cell in _cells(frame_bgr, box):
        if cell.size == 0:
            return 1e9
        patch, mask, frac = _prep(cell)
        if r in (0, 1, 6, 7):
            if frac < 0.04:
                cost += 4.0
            else:
                ys, xs = np.nonzero(mask)
                cost += (abs(ys.mean() - _PATCH / 2) + abs(xs.mean() - _PATCH / 2)) / _PATCH
        else:
            cost += 4.0 * frac
    return cost


def autotune_box(frame_bgr: np.ndarray, box, reach: int = 12):
    """Nudge the detected box so an opening frame's pieces sit dead-centre."""
    from board_detect import Box

    x, y, w, h = box.as_tuple() if hasattr(box, "as_tuple") else box
    best, best_cost = (x, y, w, h), _align_cost(frame_bgr, (x, y, w, h))
    for step in (6, 2):
        bx, by, bw, bh = best
        for dx in range(-reach, reach + 1, step):
            for dy in range(-reach, reach + 1, step):
                for ds in range(-reach, reach + 1, step * 2):
                    cand = (bx + dx, by + dy, bw + ds, bh + ds)
                    if cand[2] < 40 or cand[3] < 40:
                        continue
                    cost = _align_cost(frame_bgr, cand)
                    if cost < best_cost:
                        best_cost, best = cost, cand
    return Box(*best, score=float(best_cost))


def _build_model(cells: dict, flipped: bool) -> BoardModel:
    temps: list[np.ndarray] = []
    masks: list[np.ndarray] = []
    piece_of: list[str] = []
    per_piece: dict[str, int] = {}
    empty_patches = ([], [])            # by (r + c) parity
    for r in range(8):
        for c in range(8):
            std_i = (r * 8 + c) if not flipped else (63 - (r * 8 + c))
            piece = START_PLACEMENT[std_i]
            patch, mask, frac = _prep(cells[(r, c)])
            if piece == ".":
                empty_patches[(r + c) % 2].append(patch)
                continue
            if per_piece.get(piece, 0) >= _MAX_EXEMPLARS or frac < _MIN_PIECE_FRAC:
                continue
            temps.append(patch)
            masks.append(mask)
            piece_of.append(piece)
            per_piece[piece] = per_piece.get(piece, 0) + 1

    empty = np.stack([
        np.mean(empty_patches[0], axis=0) if empty_patches[0] else np.zeros((_PATCH, _PATCH), np.float32),
        np.mean(empty_patches[1], axis=0) if empty_patches[1] else np.zeros((_PATCH, _PATCH), np.float32),
    ])
    return BoardModel(
        temps=np.stack(temps) if temps else np.zeros((1, _PATCH, _PATCH), np.float32),
        masks=np.stack(masks) if masks else np.zeros((1, _PATCH, _PATCH), np.float32),
        piece_of=piece_of or ["."], flipped=flipped, empty=empty.astype(np.float32),
    )


_START = START_PLACEMENT
_START_FLIPPED = START_PLACEMENT[::-1]


def calibrate_from_start(frame_bgr: np.ndarray, box, tolerance: int = 6):
    """Build a piece-template model from a frame believed to show the opening.

    Determines board orientation and reads the frame back; returns (model,
    tuned_box) if it reproduces the standard position within *tolerance*
    squares, or (None, tuned_box) if not (the frame wasn't really an opening
    position - keep looking).
    """
    box = autotune_box(frame_bgr, box)
    cells = {(r, c): cell for r, c, cell in _cells(frame_bgr, box)}

    # Orientation can't be determined from *which* squares are occupied: the
    # standard opening is symmetric under a 180-degree rotation combined with
    # swapping which colour is which, so both hypotheses read back as an
    # equally valid-looking start position and a structural/hamming
    # comparison can't tell them apart (it previously always picked "normal"
    # on a tie, silently mislabelling every white/black piece whenever a
    # video actually opened on a flipped board - e.g. the recorded side
    # playing Black). The one signal that *does* disambiguate it is real
    # pixel colour: White's pieces render bright and Black's dark regardless
    # of which physical side of the screen they're on.
    top = np.mean([_piece_is_light(cells[(r, c)]) for r in (0, 1) for c in range(8)])
    bottom = np.mean([_piece_is_light(cells[(r, c)]) for r in (6, 7) for c in range(8)])
    flipped = top > bottom

    model = _build_model(cells, flipped)
    if len(set(model.piece_of)) < 12:
        # one of the 12 piece types (there's only one white queen, one black
        # king, etc. in the starting position, so each has exactly one chance
        # to be captured) never produced a usable template - e.g. a "guess
        # the opening move" ghost/preview piece animation some broadcasts
        # show right before the game starts can leave a home square looking
        # empty at the exact calibration frame. A model missing a piece type
        # entirely can never read that piece for the rest of the video (seen
        # in practice: a vanished queen silently misread as other pieces
        # throughout), so treat this as "not really a clean start" and keep
        # looking rather than accept a permanently broken model.
        return None, box
    rd = read_board(frame_bgr, box, model)
    err = min(_hamming_str(rd.placement, _START), _hamming_str(rd.placement, _START_FLIPPED))
    if err > tolerance:
        return None, box
    return model, box


def _hamming_str(a: str, b: str) -> int:
    return sum(1 for x, y in zip(a, b) if x != y)


_SHIFTS = [(dx, dy) for dx in (-4, -2, 0, 2, 4) for dy in (-4, -2, 0, 2, 4) if (dx, dy) != (0, 0)]


def _shifted_match(frame_bgr: np.ndarray, box, r: int, c: int, model: "BoardModel") -> tuple[int, float]:
    """(template index, similarity) for square (r, c) re-cropped at small
    offsets - the best over all of them."""
    x, y, w, h = box.as_tuple() if hasattr(box, "as_tuple") else box
    xe, ye = np.linspace(x, x + w, 9), np.linspace(y, y + h, 9)
    ix, iy = (w / 8) * _INSET, (h / 8) * _INSET
    y0, y1 = int(ye[r] + iy), int(ye[r + 1] - iy)
    x0, x1 = int(xe[c] + ix), int(xe[c + 1] - ix)
    best_k, best = 0, -1.0
    for dx, dy in _SHIFTS:
        if y0 + dy < 0 or x0 + dx < 0:
            continue
        cell = frame_bgr[y0 + dy:y1 + dy, x0 + dx:x1 + dx]
        if cell.size == 0:
            continue
        patch = _patch(cell)
        mask, frac = _piece_mask(patch)
        if frac < _MIN_PIECE_FRAC:
            continue
        sims = _zncc_many(patch, model.temps, np.maximum(model.masks, mask[None]))
        k = int(np.argmax(sims))
        if sims[k] > best:
            best_k, best = k, float(sims[k])
    return best_k, best


def read_board(frame_bgr: np.ndarray, box, model: BoardModel) -> Reading:
    chars: list[str] = []
    scores: list[float] = []
    occ = np.zeros((8, 8), bool)
    for r, c, cell in _cells(frame_bgr, box):
        if cell.size == 0:
            chars.append(".")
            continue
        patch = _patch(cell)
        diff = (float(np.mean(np.abs(patch - model.empty[(r + c) % 2])))
                if model.empty is not None else 99.0)
        if diff < model.occ_diff:                     # matches the empty square -> blank
            chars.append(".")
            continue
        mask, frac = _piece_mask(patch)
        if frac < _MIN_PIECE_FRAC:
            # no blob at all - without this, ZNCC falls back to the *template's*
            # mask below and can score a spurious match against plain background
            # texture (wood grain, a shadow, a stray cursor) with nothing of its
            # own to disprove it
            chars.append(".")
            continue
        occ[r, c] = True
        eff = np.maximum(model.masks, mask[None])
        sims = _zncc_many(patch, model.temps, eff)
        k = int(np.argmax(sims))
        if sims[k] < model.match_thresh:
            # A clear piece blob that matches nothing may just sit a few pixels
            # off the grid (confirmed on a real video: a bishop read as empty
            # at sim 0.14 matched at 0.83 three pixels higher, while shifting
            # the whole board made every other square worse). Retry this one
            # square slightly shifted before giving up on it.
            k, best = _shifted_match(frame_bgr, box, r, c, model)
            if best >= model.match_thresh:
                chars.append(model.piece_of[k])
                scores.append(best)
                continue
        if sims[k] < model.match_thresh:
            # a real, sizeable blob (frac test above passed) that doesn't
            # confidently match any template - e.g. a piece the mouse cursor
            # is hovering over/dragging, distorting its shape - but also,
            # empirically, just an ordinary marginal match on some videos'
            # rendering. Tried marking this "?" (occupied, unknown) and
            # treating it as a wildcard in reconstruct._hamming instead of
            # blank: net worse on a validated test video, where it turned out
            # *most* frames had at least one such cell, so the "wildcard"
            # became broad extra slack everywhere rather than a rare,
            # targeted allowance - reverted. See scanner/tests/test_ground_truth.py.
            chars.append(".")
            continue
        chars.append(model.piece_of[k])
        scores.append(float(sims[k]))

    placement = "".join(chars)
    return Reading(placement, float(np.mean(scores)) if scores else 0.0,
                   sum(ch != "." for ch in placement), occ)
