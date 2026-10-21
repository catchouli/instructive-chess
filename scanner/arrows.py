"""Arrows the narrator draws on the board (prototype).

Streamers point at moves while they talk ("try to go here, do this,
checkmate" over an arrow h2-h6), so an arrow turns vague commentary into a
concrete line. This reads them off one frame, given the board box the
scanner already found.

Two styles:
- chess.com: solid, saturated colours (orange by default); the tail starts
  near the edge of the origin square, the tip is at the destination's centre;
  knight arrows are L-shaped (two squares along, one across).
- lichess (chessground, open source): lines 10/64 of a square wide from the
  origin's centre, the tip at the destination's centre, drawn translucent
  over the board; knight arrows are straight.

Each candidate arrow along a queen or knight line is scored by how much of its
shaft is arrow-coloured, whether its head is there (wider than the shaft) and
whether the colour stops past its tip. Arrows are taken best first.

    scanner/.venv/bin/python scanner/arrows.py IMAGE X Y W H [--flipped] [--style chesscom|lichess]
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass

import cv2
import numpy as np

# brush colours, RGB
CHESSCOM = {"orange": (255, 167, 0)}
LICHESS = {"green": (0x15, 0x78, 0x1B), "red": (0x88, 0x20, 0x20), "blue": (0x00, 0x30, 0x88), "yellow": (0xE6, 0x8F, 0x00)}
LICHESS_ALPHA = 0.6        # fits real frames (35 hand-labelled)
LICHESS_HIGHLIGHT = (155, 199, 0)   # the last move's squares, at 0.41


@dataclass(frozen=True)
class Style:
    name: str
    colours: dict
    alpha: float
    shaft: float           # width, in squares
    head_width: float
    head_len: float
    tail_start: float      # how far from the origin's centre the tail starts
    knight_l: bool


STYLES = {
    "chesscom": Style("chesscom", CHESSCOM, 1.0, 0.21, 0.45, 0.30, 0.44, True),
    "lichess": Style("lichess", LICHESS, LICHESS_ALPHA, 10 / 64, 4 * 10 / 64, 3 * 10 / 64, 0.0, False),
}

KNIGHT = [(1, 2), (2, 1), (2, -1), (1, -2), (-1, -2), (-2, -1), (-2, 1), (-1, 2)]
LINES = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]


@dataclass(frozen=True)
class Arrow:
    origin: str       # e.g. "h2"
    dest: str
    colour: str
    score: float

    def __str__(self) -> str:
        return f"{self.origin}{self.dest}"


def square_name(f: int, r: int) -> str:
    return "abcdefgh"[f] + str(r + 1)


def candidates() -> list[tuple[int, int, int, int]]:
    out = []
    for f in range(8):
        for r in range(8):
            for df, dr in LINES:
                for k in range(1, 8):
                    f2, r2 = f + df * k, r + dr * k
                    if 0 <= f2 < 8 and 0 <= r2 < 8:
                        out.append((f, r, f2, r2))
            for df, dr in KNIGHT:
                if 0 <= f + df < 8 and 0 <= r + dr < 8:
                    out.append((f, r, f + df, r + dr))
    return out


CANDIDATES = candidates()


def centre(f: int, r: int, flipped: bool) -> np.ndarray:
    """Square centre in board units (squares from the top left corner)."""
    if flipped:
        return np.array([7 - f + 0.5, r + 0.5])
    return np.array([f + 0.5, 7 - r + 0.5])


def path(c: tuple[int, int, int, int], style: Style, flipped: bool) -> list[np.ndarray]:
    """The arrow's centre line, as points in board units: origin, (the knight's
    corner,) destination."""
    f, r, f2, r2 = c
    a, b = centre(f, r, flipped), centre(f2, r2, flipped)
    if style.knight_l and abs(f2 - f) * abs(r2 - r) == 2:
        # two squares along the long side first, then one across
        if abs(f2 - f) == 2:
            corner = centre(f2, r, flipped)
        else:
            corner = centre(f, r2, flipped)
        return [a, corner, b]
    return [a, b]


def colour_masks(board_rgb: np.ndarray, style: Style) -> dict[str, np.ndarray]:
    """Per brush colour, the pixels that look like it drawn over the board.

    Translucent arrows (lichess) are the brush blended with what's beneath:
    the light and dark square colours are estimated from the squares'
    corners, which pieces rarely cover."""
    h, w = board_rgb.shape[:2]
    img = board_rgb.astype(np.float32)
    if style.alpha < 1:
        sq = w / 8
        light, dark = [], []
        for f in range(8):
            for r in range(8):
                y0, x0 = int(r * sq + 2), int(f * sq + 2)
                (light if (f + r) % 2 == 0 else dark).append(img[y0:y0 + 4, x0:x0 + 4].reshape(-1, 3))
        squares = [np.median(np.concatenate(light), axis=0), np.median(np.concatenate(dark), axis=0)]
        bases = squares + [
            # ...over the pieces' bodies: an arrow starts on the piece it
            # moves, so a short one is half over it
            np.array([245, 245, 245], np.float32), np.array([40, 40, 40], np.float32)] + [
            # ...and over the last move's highlighted squares (lichess:
            # rgba(155, 199, 0, 0.41)), which on their own are plain board
            0.41 * np.array(LICHESS_HIGHLIGHT, np.float32) + 0.59 * b for b in squares]
    out = {}
    for name, rgb in style.colours.items():
        c = np.array(rgb, np.float32)
        if style.alpha >= 1:
            targets = [c]
        else:
            targets = [style.alpha * c + (1 - style.alpha) * b for b in bases]
        d = np.min([np.linalg.norm(img - t, axis=2) for t in targets], axis=0)
        if style.alpha >= 1:
            out[name] = d < 40
        else:
            # a translucent brush can blend into something close to the board
            # itself (red over a brown wood board, red over a black piece):
            # an arrow pixel must also stand apart from the plain colours
            plain = np.min([np.linalg.norm(img - b, axis=2) for b in bases], axis=0)
            out[name] = (d < 40) & (plain > 30)
    return out


def _sample(mask: np.ndarray, pts: np.ndarray, sq: float) -> np.ndarray:
    h, w = mask.shape
    xs = np.clip((pts[:, 0] * sq).astype(int), 0, w - 1)
    ys = np.clip((pts[:, 1] * sq).astype(int), 0, h - 1)
    inside = (pts[:, 0] >= 0) & (pts[:, 0] < 8) & (pts[:, 1] >= 0) & (pts[:, 1] < 8)
    return mask[ys, xs] & inside


def score(mask: np.ndarray, c, style: Style, flipped: bool) -> float:
    """0..1: how well the arrow along candidate *c* explains *mask*."""
    sq = mask.shape[1] / 8
    pts = path(c, style, flipped)
    segs = list(zip(pts[:-1], pts[1:]))
    shaft = []
    for i, (p, q) in enumerate(segs):
        d = q - p
        n = np.linalg.norm(d)
        u = d / n
        lo = style.tail_start if i == 0 else 0.0
        hi = n - (style.head_len if i == len(segs) - 1 else 0.0)
        if hi - lo < 0.1:
            continue
        ts = np.linspace(lo, hi, max(4, int((hi - lo) * 12)))
        shaft.append(p + ts[:, None] * u)
    if not shaft:
        return 0.0
    shaft = np.concatenate(shaft)
    cover = _sample(mask, shaft, sq).mean()
    p, q = segs[-1]
    u = (q - p) / np.linalg.norm(q - p)
    nrm = np.array([-u[1], u[0]])
    # the head: wider than the shaft near its base (a triangle, so measured
    # where it's still most of its full width)
    t = 0.85
    base = q - u * (style.head_len * t)
    off = (style.shaft / 2 + style.head_width / 2 * t) / 2
    head = _sample(mask, np.array([base + nrm * off, base - nrm * off]), sq).mean()
    # and the tail end is narrow: on a translucent arrow the head is faint,
    # so this is what tells e3-b6 from b6-e3
    p0, q0 = segs[0]
    u0 = (q0 - p0) / np.linalg.norm(q0 - p0)
    n0 = np.array([-u0[1], u0[0]])
    tail_at = p0 + u0 * (style.tail_start + style.head_len * (1 - t))
    tail = _sample(mask, np.array([tail_at + n0 * off, tail_at - n0 * off]), sq).mean()
    # nothing past the tip, nor before the tail (that would be a longer arrow).
    # Past the tip it must carry straight on for most of a square: another
    # arrow into the same square (a capture and its recapture) only puts its
    # head there.
    beyond = float(_sample(mask, np.array([q + u * k for k in (0.35, 0.65, 0.95)]), sq).all())
    before = _sample(mask, np.array([p0 + u0 * (style.tail_start - k) for k in (0.25, 0.4)]), sq).mean()
    # thin: the colour stops just beside the shaft (a highlighted square,
    # translucent green on lichess, is filled edge to edge)
    mid = shaft[len(shaft) // 4: 3 * len(shaft) // 4]
    side = 0.0
    if len(mid):
        p1, q1 = segs[0]
        u1 = (q1 - p1) / np.linalg.norm(q1 - p1)
        n1 = np.array([-u1[1], u1[0]]) * (style.shaft / 2 + 0.12)
        side = _sample(mask, np.concatenate([mid + n1, mid - n1]), sq).mean()
    return float(cover * (0.5 + 0.5 * head) * (1 - 0.3 * tail) * (1 - 0.7 * beyond) * (1 - 0.5 * before)
                 * (1 - 0.7 * side))


def detect(board_bgr: np.ndarray, flipped: bool, style_name: str, threshold: float = 0.6) -> list[Arrow]:
    """The arrows drawn on a board crop (BGR, the board box exactly)."""
    style = STYLES[style_name]
    rgb = cv2.cvtColor(board_bgr, cv2.COLOR_BGR2RGB)
    found = []
    for colour, mask in colour_masks(rgb, style).items():
        if mask.mean() < 0.002:
            continue
        mask = mask.copy()
        sq = mask.shape[1] / 8
        for _ in range(6):
            scored = [(score(mask, c, style, flipped), c) for c in CANDIDATES]
            best, c = max(scored)
            if best < threshold:
                break
            f, r, f2, r2 = c
            found.append(Arrow(square_name(f, r), square_name(f2, r2), colour, round(best, 3)))
            # explained: clear the arrow's own pixels (its shaft and head, no
            # wider) so the next one can be found - arrows often meet on a square
            pts = path(c, style, flipped)
            m8 = mask.view(np.uint8)
            for p, q in zip(pts[:-1], pts[1:]):
                cv2.line(m8, tuple(int(v) for v in p * sq), tuple(int(v) for v in q * sq), 0,
                         thickness=max(2, int(style.shaft * 1.6 * sq)))
            u = (pts[-1] - pts[-2]) / np.linalg.norm(pts[-1] - pts[-2])
            nrm = np.array([-u[1], u[0]])
            base = pts[-1] - u * style.head_len * 1.1
            tri = np.array([pts[-1] + u * 0.05, base + nrm * style.head_width * 0.6, base - nrm * style.head_width * 0.6])
            cv2.fillConvexPoly(m8, (tri * sq).astype(np.int32), 0)
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image")
    ap.add_argument("box", nargs=4, type=int)
    ap.add_argument("--flipped", action="store_true")
    ap.add_argument("--style", choices=sorted(STYLES), default="chesscom")
    args = ap.parse_args(argv)
    img = cv2.imread(args.image)
    x, y, w, h = args.box
    for a in detect(img[y:y + h, x:x + w], args.flipped, args.style):
        print(a, a.colour, a.score)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
