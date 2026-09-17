#!/usr/bin/env python3
"""Scan an instructive chess video and catalogue the games it contains.

    scanner/.venv/bin/python scanner/scan.py <youtube-url|local-file> [options]

Downloads the video (cached under scanner/cache/), samples frames, locates the
on-screen board, self-calibrates piece templates from the opening position, reads
each frame, reconstructs the games move-by-move, and appends them to
web/data/games.json for the static web app.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board_detect          # noqa: E402
import catalog               # noqa: E402
import clock_read            # noqa: E402
import download              # noqa: E402
import recognize             # noqa: E402
import reconstruct           # noqa: E402

CLOCK_MIN_COVERAGE = 0.5   # a candidate clock region must read at least this
                           # fraction of its window's frames to be trusted
CLOCK_MIN_FRAMES = 20      # a window shorter than this can't bootstrap a
                           # wrap-anchor (see clock_read.py) reliably enough
                           # to be worth attempting at all


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="scan.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("target", help="YouTube URL or path to a local video file")
    p.add_argument("--fps", type=float, default=1.0, help="frames sampled per second (default 1.0)")
    p.add_argument("--max-height", type=int, default=720, help="cap download resolution (default 720)")
    p.add_argument("--start", type=float, default=0.0, help="start time in seconds")
    p.add_argument("--end", type=float, default=0.0, help="end time in seconds (0 = to end)")
    p.add_argument("--crop", default="", help="board box 'x,y,w,h' (skip auto-detection)")
    p.add_argument("--calibrate", action="store_true", help="write an annotated frame and exit")
    p.add_argument("--min-confidence", type=float, default=0.6,
                   help="drop games below this reconstruction confidence (default 0.6)")
    p.add_argument("--dry-run", action="store_true", help="print games, do not write games.json")
    p.add_argument("--no-clocks", action="store_true", help="skip reading on-screen clocks")
    return p.parse_args(argv)


def iter_frames(path: Path, fps: float, start: float, end: float):
    """Yield (timestamp, frame_bgr) at ~fps. ffmpeg decodes and downsamples the
    whole video in one pass - far faster than per-sample seeking in OpenCV."""
    tmp = Path(tempfile.mkdtemp(prefix="instructive-chess-"))
    try:
        cmd = ["ffmpeg", "-nostdin", "-v", "error"]
        if start > 0:
            cmd += ["-ss", f"{start:.3f}"]
        if end > 0:
            cmd += ["-to", f"{end:.3f}"]
        cmd += ["-i", str(path), "-vf", f"fps={fps}", "-qscale:v", "2",
                str(tmp / "f_%07d.jpg")]
        subprocess.run(cmd, check=True)
        for i, fp in enumerate(sorted(tmp.glob("f_*.jpg"))):
            img = cv2.imread(str(fp))
            if img is not None:
                yield start + i / fps, img
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def sample_frames(path: Path, start: float, end: float, n: int):
    cap = cv2.VideoCapture(str(path))
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    a = int(start * src_fps)
    b = int(end * src_fps) if end > 0 else int(total)
    a += int(0.05 * (b - a))                      # skip intro
    out = []
    for k in range(n):
        f = int(a + (b - a) * (k + 0.5) / max(n, 1))
        cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, frame = cap.read()
        if ok:
            out.append(frame)
    cap.release()
    return out


def fmt_clock(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def annotate_clocks(path: Path, board_box, games: list[reconstruct.Game]) -> None:
    """Best-effort: read each game's own on-screen clock(s) over just that
    game's video window - bounded memory regardless of how long the source
    video is, unlike decoding the whole thing into clock_read at once - and
    attach remaining-time-per-move to the game object.

    Leaves clock_*_seconds at their empty default (see reconstruct.Game) for
    any game this can't do confidently for: a missing clock reading next to
    a real move is far less harmful than a wrong one, and clock_read.py is
    already deliberately conservative about reporting a value at all (see
    its module docstring).
    """
    for g in games:
        if not g.move_seconds:
            continue
        start = max(0.0, g.start_seconds - 1.0)
        end = g.move_seconds[-1] + 3.0
        frames, times = [], []
        for ts, frame in iter_frames(path, 1.0, start, end):
            frames.append(frame)
            times.append(ts)
        if len(frames) < CLOCK_MIN_FRAMES:
            continue
        for cbox in clock_read.find_clock_regions(frames, board_box):
            readings = clock_read.read_clock(frames, times, cbox)
            frac = sum(1 for r in readings if r.seconds is not None) / len(readings)
            if frac < CLOCK_MIN_COVERAGE:
                continue
            side = clock_read.side_for_box(cbox, board_box, g.flipped)
            values = clock_read.clock_at_times(readings, g.move_seconds)
            if side == "white":
                g.clock_white_seconds = values
            else:
                g.clock_black_seconds = values


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    print(f"[1/5] resolving {args.target}")
    src = download.fetch(args.target, max_height=args.max_height)
    print(f"      {src.title!r} - {src.channel or 'unknown channel'}  ({src.path.name})")

    print("[2/5] locating board")
    if args.crop:
        x, y, w, h = (int(v) for v in args.crop.split(","))
        box = board_detect.Box(x, y, w, h, score=1.0)
    else:
        box = board_detect.detect(sample_frames(src.path, args.start, args.end, 15))
        if box is None:
            print("      board not found - try --crop x,y,w,h or --calibrate", file=sys.stderr)
            return 2
    print(f"      box x={box.x} y={box.y} {box.w}x{box.h} score={box.score:.3f}")

    if args.calibrate:
        mids = sample_frames(src.path, args.start, args.end, 1)
        if not mids:
            print("      could not read any frames", file=sys.stderr)
            return 2
        mid = mids[0]
        out = download.CACHE / f"{src.video_id}-calib.png"
        cv2.imwrite(str(out), board_detect.draw(mid, box))
        print(f"      wrote {out}")
        return 0

    print(f"[3/5] scanning frames @ {args.fps} fps")
    model: recognize.BoardModel | None = None
    readings: list[tuple[float, str | None]] = []
    n_board = 0
    for ts, frame in iter_frames(src.path, args.fps, args.start, args.end):
        if model is None:
            cand = None
            if recognize.looks_like_start(recognize.occupancy(frame, box)):
                cand, tuned = recognize.calibrate_from_start(frame, box)
            if cand is None:
                readings.append((ts, None))
                continue
            model, box = cand, tuned
            print(f"      calibrated at t={int(ts)}s "
                  f"(board {'flipped' if model.flipped else 'normal'}, "
                  f"box {box.x},{box.y} {box.w}x{box.h}, "
                  f"{len(model.piece_of)} templates)")
        rd = recognize.read_board(frame, box, model)
        # a spurious extra piece or two is fine here - reconstruction will reject
        # an impossible position as a skip; a hard None here would look like a gap
        if rd.confidence >= 0.72 and 4 <= rd.n_pieces <= 34:
            readings.append((ts, rd.placement))
            n_board += 1
        else:
            readings.append((ts, None))

    if model is None:
        print("      never saw an opening position - no games recorded", file=sys.stderr)
        return 3
    print(f"      {len(readings)} frames, {n_board} with a readable board")

    print("[4/5] reconstructing games")
    readings = recognize.vote_stream(readings)
    games = reconstruct.reconstruct(readings)
    kept = [g for g in games if g.confidence >= args.min_confidence and g.ply >= 2]
    print(f"      {len(games)} games found, {len(kept)} kept "
          f"(confidence >= {args.min_confidence})")

    if args.no_clocks:
        print("[5/5] reading clocks - skipped (--no-clocks)")
    else:
        print("[5/5] reading clocks")
        annotate_clocks(src.path, box, kept)
        n_clocked = sum(1 for g in kept if g.clock_white_seconds or g.clock_black_seconds)
        print(f"      clock data for {n_clocked}/{len(kept)} games")

    for i, g in enumerate(kept):
        flip = " [flipped]" if g.flipped else ""
        clocks = ""
        if g.clock_white_seconds or g.clock_black_seconds:
            w = fmt_clock(g.clock_white_seconds[-1]) if g.clock_white_seconds else "?"
            b = fmt_clock(g.clock_black_seconds[-1]) if g.clock_black_seconds else "?"
            clocks = f"  clocks(end) W={w} B={b}"
        print(f"\n  game {i + 1}: {g.ply} plies  conf={g.confidence:.2f}  "
              f"t={int(g.start_seconds)}s{flip}{clocks}")
        print(f"    {g.pgn()[:240]}")

    if args.dry_run:
        print("\n(dry run - games.json not modified)")
        return 0

    added, total = catalog.merge(src, kept)
    print(f"\nwrote {catalog.DATA_FILE}  (+{added} for this video, {total} total)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
