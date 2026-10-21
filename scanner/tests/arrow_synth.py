#!/usr/bin/env python3
"""Synthetic test of arrows.py: arrows drawn in each site's style onto real
board frames from the cache (so the right answer is known), then detected.

    scanner/.venv/bin/python scanner/tests/arrow_synth.py [--frames 40] [--seed 1] [--out DIR]

Frames come from videos whose games were played on that site (benchmark
platform), spread evenly over the opening (plies 1-16), middlegame (17-50)
and late game (51+), and only where the game's moves are trusted, so the
position under the arrows is known. Each frame gets 0-3 arrows like a
narrator's: mostly the next moves actually played, sometimes another legal
move from the position, now and then any queen or knight line - so they
start on pieces, cross them, capture and castle. A random brush colour each.
Frames with none measure false alarms (but a real frame may already carry an
arrow - see --out to look). Prints precision and recall per style and phase.
"""
from __future__ import annotations

import argparse
import collections
import json
import random
import subprocess
import sys
from pathlib import Path

import chess
import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import arrows  # noqa: E402

CACHE = Path(__file__).resolve().parents[1] / "cache"
BENCH = CACHE / "handoff" / "merge2" / "bm.json"


def videos_by_site() -> dict[str, list[str]]:
    by: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for r in json.loads(BENCH.read_text())["rows"]:
        if r.get("matched"):
            by[r["video_id"]][r["platform"]] += 1
    out = {"lichess": [], "chesscom": []}
    for v, c in sorted(by.items()):
        if not (CACHE / f"{v}.mkv").exists() or not (CACHE / f"{v}-readings.json").exists():
            continue
        out["lichess" if c["lichess"] > c["chess.com"] else "chesscom"].append(v)
    return out


def frame(video: str, t: float) -> np.ndarray | None:
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(CACHE / f"{video}.mkv"),
                          "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True).stdout
    return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR) if raw else None


PHASES = {"opening": (1, 16), "middlegame": (17, 50), "late": (51, 999)}


def move_squares(board: chess.Board, mv: chess.Move) -> tuple[int, int, int, int]:
    return (chess.square_file(mv.from_square), chess.square_rank(mv.from_square),
            chess.square_file(mv.to_square), chess.square_rank(mv.to_square))


def narrator_arrows(game: dict, ply: int, rng: random.Random) -> list[tuple[int, int, int, int]]:
    """0-3 arrows a narrator might draw on the position after *ply* plies."""
    board = chess.Board()
    for san in game["moves_san"][:ply]:
        board.push_san(san)
    n = rng.choice([0, 1, 1, 2, 3])
    out: list[tuple[int, int, int, int]] = []
    future = board.copy()
    for san in game["moves_san"][ply:ply + n]:      # the line that was played
        mv = future.parse_san(san)
        out.append(move_squares(future, mv))
        future.push(mv)
    for i in range(len(out)):
        r = rng.random()
        if r < 0.25 and list(board.legal_moves):      # another move from here
            out[i] = move_squares(board, rng.choice(list(board.legal_moves)))
        elif r < 0.35:                                # any line at all
            out[i] = rng.choice(arrows.CANDIDATES)
    return [c for c in dict.fromkeys(out) if c in set(arrows.CANDIDATES)]


def draw(board_bgr: np.ndarray, c, colour: str, style: arrows.Style, flipped: bool, layer: np.ndarray) -> None:
    """Draw one arrow onto *layer* (a float mask, 1 = covered) and, for an
    opaque style, onto the board itself."""
    sq = board_bgr.shape[1] / 8
    pts = arrows.path(c, style, flipped)
    u_last = (pts[-1] - pts[-2]) / np.linalg.norm(pts[-1] - pts[-2])
    u_first = (pts[1] - pts[0]) / np.linalg.norm(pts[1] - pts[0])
    line = [pts[0] + u_first * style.tail_start] + pts[1:-1] + [pts[-1] - u_last * style.head_len]
    m = np.zeros(board_bgr.shape[:2], np.uint8)
    px = [tuple(int(round(v)) for v in p * sq) for p in line]
    for a, b in zip(px[:-1], px[1:]):
        cv2.line(m, a, b, 1, thickness=max(1, int(round(style.shaft * sq))), lineType=cv2.LINE_AA)
    tip, base = pts[-1], pts[-1] - u_last * style.head_len
    nrm = np.array([-u_last[1], u_last[0]])
    tri = np.array([tip, base + nrm * style.head_width / 2, base - nrm * style.head_width / 2]) * sq
    cv2.fillConvexPoly(m, tri.astype(np.int32), 1, lineType=cv2.LINE_AA)
    layer[m > 0] = 1.0


def compose(board_bgr: np.ndarray, layers: list[tuple[np.ndarray, str]], style: arrows.Style) -> np.ndarray:
    out = board_bgr.astype(np.float32)
    for layer, colour in layers:
        rgb = np.array(style.colours[colour], np.float32)[::-1]      # to BGR
        a = (layer * style.alpha)[..., None]
        out = out * (1 - a) + rgb * a
    return np.clip(out, 0, 255).astype(np.uint8)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--frames", type=int, default=40, help="per style")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", type=Path, help="save each test image here")
    args = ap.parse_args(argv)
    rng = random.Random(args.seed)
    games = json.loads((CACHE.parent.parent / "web" / "data" / "games.json").read_text())
    vids = videos_by_site()
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
    for site in ("chesscom", "lichess"):
        style = arrows.STYLES[site]
        tp = fp = fn = 0
        by_phase: dict[str, list[int]] = {p: [0, 0, 0] for p in PHASES}
        real_arrows = 0
        misses = []
        n = 0
        while n < args.frames:
            phase = list(PHASES)[n % len(PHASES)]
            v = rng.choice(vids[site])
            box = json.loads((CACHE / f"{v}-readings.json").read_text())["box"]
            lo, hi = PHASES[phase]
            gs = [g for g in games if g["video_id"] == v and min(g.get("trusted_ply", 0), len(g["moves_san"]) - 1) >= lo]
            if not gs:
                continue
            g = rng.choice(gs)
            top = min(hi, g.get("trusted_ply", 0), len(g["moves_san"]) - 1)
            ply = rng.randint(lo, top)
            # the position after `ply` plies is on screen from that move until the next
            t = rng.uniform(g["move_seconds"][ply - 1] + 0.3, max(g["move_seconds"][ply - 1] + 0.4, g["move_seconds"][ply] - 0.3))
            img = frame(v, t)
            if img is None:
                continue
            x, y, w, h = box[:4]
            board = img[y:y + h, x:x + w]
            if board.shape[0] < 100 or board.shape[1] < 100:
                continue
            if arrows.detect(board, g["flipped"], site):
                # the narrator's own arrows are already on it: no clean truth
                real_arrows += 1
                if args.out:
                    cv2.imwrite(str(args.out / f"{site}_real_{v}_{int(t)}.png"), board)
                continue
            truth, layers = set(), []
            for c in narrator_arrows(g, ply, rng):
                colour = rng.choice(sorted(style.colours))
                layer = np.zeros(board.shape[:2], np.float32)
                draw(board, c, colour, style, g["flipped"], layer)
                layers.append((layer, colour))
                truth.add(arrows.square_name(c[0], c[1]) + arrows.square_name(c[2], c[3]))
            test = compose(board, layers, style)
            got = {str(a) for a in arrows.detect(test, g["flipped"], site)}
            tp += len(got & truth)
            fp += len(got - truth)
            fn += len(truth - got)
            for i, k in enumerate((len(got & truth), len(got - truth), len(truth - got))):
                by_phase[phase][i] += k
            if got != truth:
                misses.append(f"{phase} {v} {t:.0f}s ply {ply}: drew {sorted(truth)}, found {sorted(got)}")
            if args.out:
                cv2.imwrite(str(args.out / f"{site}_{n:03d}_{phase}_{v}_{int(t)}.png"), test)
            n += 1
        prec = tp / (tp + fp) if tp + fp else 1.0
        rec = tp / (tp + fn) if tp + fn else 1.0
        print(f"{site}: {args.frames} frames, precision {tp}/{tp + fp} ({100 * prec:.0f}%), recall {tp}/{tp + fn} ({100 * rec:.0f}%)")
        print(f"    frames skipped, already showing arrows: {real_arrows} of {args.frames + real_arrows}")
        for p, (a, b, c) in by_phase.items():
            print(f"    {p:10} precision {a}/{a + b}, recall {a}/{a + c}")
        for m in misses[:12]:
            print("   ", m)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
