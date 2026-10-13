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
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import chess
import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board_detect          # noqa: E402
import catalog               # noqa: E402
import clock_read            # noqa: E402
import download              # noqa: E402
import rating_read           # noqa: E402
import recognize             # noqa: E402
import reconstruct           # noqa: E402
import refine                # noqa: E402

CLOCK_MIN_COVERAGE = 0.35  # a candidate clock region must read at least this
                           # fraction of its window's frames to be trusted - an
                           # idle clock reads poorly in some skins, and misreads
                           # are blanked by clock_read._physically_consistent
CLOCK_MAX_CANDIDATES = 4  # candidate regions screened per game; the best-reading two are kept
CLOCK_SCREEN_FRAMES = 40  # frames each candidate is screened on before a full read
RATING_BOX_SHIFTS = (-3, 0, 3)  # board-box offsets (scan pixels) each rating frame is read at
CLOCK_EARLY_FRAMES = 240  # ...and from this many opening frames, where the clocks are easiest to find
CLOCK_MIN_FRAMES = 20      # a window shorter than this can't bootstrap a
                           # wrap-anchor (see clock_read.py) reliably enough
                           # to be worth attempting at all


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="scan.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("target", help="YouTube URL or path to a local video file")
    p.add_argument("--fps", type=float, default=1.0,
                   help="frames sampled per second (default 1). Whole-video 2 fps was tried: it split "
                        "fast moves apart but halved recognize.vote_stream's flicker window (a fixed "
                        "frame count), and on the full library more games went wrong than right")
    p.add_argument("--max-height", type=int, default=720, help="cap download resolution (default 720)")
    p.add_argument("--start", type=float, default=0.0, help="start time in seconds")
    p.add_argument("--end", type=float, default=0.0, help="end time in seconds (0 = to end)")
    p.add_argument("--crop", default="", help="board box 'x,y,w,h' (skip auto-detection)")
    p.add_argument("--calibrate", action="store_true", help="write an annotated frame and exit")
    p.add_argument("--min-confidence", type=float, default=0.6,
                   help="drop games below this reconstruction confidence (default 0.6)")
    p.add_argument("--min-ply", type=int, default=10,
                   help="drop games shorter than this (default 10) - measured against real games, "
                        "every catalogued 'game' under 6 plies was a junk fragment (an intro or recap "
                        "position), while the shortest real game was 15 plies")
    p.add_argument("--dry-run", action="store_true", help="print games, do not write games.json")
    p.add_argument("--catalog", type=Path, default=catalog.DATA_FILE,
                   help="catalog file to write (default web/data/games.json) - point experiments "
                        "at a scratch copy, then score it with tests/benchmark.py --catalog")
    p.add_argument("--from-readings", action="store_true",
                   help="reuse the board readings a previous scan saved (scanner/cache/<id>-readings.json) "
                        "instead of reading every frame again - for re-running reconstruction after a change")
    p.add_argument("--no-refine", action="store_true",
                   help="skip re-reading fast-move moments at a higher frame rate (see refine.py)")
    p.add_argument("--no-clocks", action="store_true", help="skip reading on-screen clocks")
    p.add_argument("--no-ratings", action="store_true", help="skip reading player ratings")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--speedrun", dest="speedrun", action="store_true", default=None,
                   help="tag the games as speedrun games (default: decided from the video title, see catalog.is_speedrun)")
    g.add_argument("--no-speedrun", dest="speedrun", action="store_false",
                   help="tag the games as not speedrun games")
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


def read_frames(frames, box, log=lambda msg: None):
    """Read every (timestamp, frame) into a (timestamp, placement-or-None)
    stream: self-calibrate piece templates at the first opening position
    (see recognize.calibrate_from_start), re-fit the board box at every
    later one (recognize.retune_at_start - streams shift the board between
    games), then match each frame. Returns (model, box, readings); model is
    None if no opening position was ever seen. Shared by scan.py and
    tests/dump_readings.py so they can't drift apart."""
    model: recognize.BoardModel | None = None
    readings: list[tuple[float, str | None]] = []
    retune_failed_at = -1e9   # a re-fit that found nothing isn't retried for a while
    for ts, frame in frames:
        if model is None:
            cand = None
            if recognize.looks_like_start(recognize.occupancy(frame, box)):
                cand, tuned = recognize.calibrate_from_start(frame, box)
            if cand is None:
                readings.append((ts, None))
                continue
            model, box = cand, tuned
            log(f"      calibrated at t={int(ts)}s "
                f"(board {'flipped' if model.flipped else 'normal'}, "
                f"box {box.x},{box.y} {box.w}x{box.h}, "
                f"{len(model.piece_of)} templates)")
        rd = recognize.read_board(frame, box, model)
        start_err = recognize.start_error(rd.placement)
        if start_err and recognize.looks_like_start_reading(rd.placement) and ts - retune_failed_at > 60:
            # (reading first and testing the result costs nothing on the
            # ordinary frame, unlike an occupancy pre-check of every frame;
            # the re-fit itself is slow, so a failed one isn't retried for
            # a minute - the next game brings its own start position)
            retuned = recognize.retune_at_start(frame, box, model, known_err=start_err)
            if retuned.as_tuple() != box.as_tuple():
                log(f"      board moved: box {box.x},{box.y} -> {retuned.x},{retuned.y} "
                    f"{retuned.w}x{retuned.h} at t={int(ts)}s")
                box = retuned
                rd = recognize.read_board(frame, box, model)
            else:
                retune_failed_at = ts
        # a spurious extra piece or two is fine here - reconstruction will reject
        # an impossible position as a skip; a hard None here would look like a gap
        if rd.confidence >= 0.72 and 4 <= rd.n_pieces <= 34:
            readings.append((ts, rd.placement))
        else:
            readings.append((ts, None))
    return model, box, readings


def fmt_clock(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def _game_over_after(moves_san: list[str], n: int) -> bool:
    board = chess.Board()
    for san in moves_san[:n]:
        board.push_san(san)
    return board.is_checkmate() or board.is_stalemate()


def _overlaps(a, b) -> bool:
    """Do two clock boxes share most of the smaller one's area?"""
    x0, y0 = max(a.x, b.x), max(a.y, b.y)
    x1, y1 = min(a.x + a.w, b.x + b.w), min(a.y + a.h, b.y + b.h)
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    return inter >= 0.5 * min(a.w * a.h, b.w * b.h)


def annotate_clocks(path: Path, board_box, games: list[reconstruct.Game]) -> None:
    """Best-effort: read each game's own on-screen clock(s) over just that
    game's video window - bounded memory regardless of how long the source
    video is, unlike decoding the whole thing into clock_read at once.

    Leaves clock_*_seconds at their empty default (see reconstruct.Game) for
    any game this can't read a clock confidently for: a missing clock
    reading next to a real move is far less harmful than a wrong one, and
    clock_read.py is already deliberately conservative about reporting a
    value at all (see its module docstring).
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

        # Screen every candidate on a sample of frames, then read only the
        # chosen clocks in full - OCR per frame is the slow part.
        step = max(1, len(frames) // CLOCK_SCREEN_FRAMES)
        sample_f, sample_t = frames[::step], times[::step]
        # Over a long game other changing UI near a clock (a chat, a stream
        # timer) can merge with it into one wide blob that reads nothing
        # (confirmed on a stream: the top clock was found over the first
        # four minutes and lost over the whole game). Pool the candidates
        # from the whole window and from its opening minutes.
        cands = list(clock_read.find_clock_regions(frames, board_box, max_regions=CLOCK_MAX_CANDIDATES))
        if len(frames) > CLOCK_EARLY_FRAMES:
            for cbox in clock_read.find_clock_regions(frames[:CLOCK_EARLY_FRAMES], board_box,
                                                      max_regions=CLOCK_MAX_CANDIDATES):
                if not any(_overlaps(cbox, c) for c in cands):
                    cands.append(cbox)
        screened = [(cbox, clock_read.read_clock_ocr(sample_f, sample_t, cbox, reject_outliers=False))
                    for cbox in cands]
        chosen = clock_read.assign_clock_sides(screened, board_box, g.flipped, CLOCK_MIN_COVERAGE)
        box_for = {id(rd): cbox for cbox, rd in screened}
        if len(chosen) == 2:
            g.clock_boxes = sorted(((int(c.x), int(c.y), int(c.w), int(c.h))
                                    for c in (box_for[id(rd)] for rd in chosen.values())),
                                   key=lambda b: b[1])
        clock_readings = {}
        for side, rd in chosen.items():
            full = clock_read.read_clock_ocr(frames, times, box_for[id(rd)])
            if sum(r.seconds is not None for r in full) >= CLOCK_MIN_COVERAGE * len(full):
                clock_readings[side] = full
        if not clock_readings:
            continue

        keep = clock_read.live_move_count(g.move_seconds, clock_readings,
                                          lambda n: _game_over_after(g.moves_san, n))
        if keep < len(g.moves_san):
            g.moves_san = g.moves_san[:keep]
            g.move_seconds = g.move_seconds[:keep]

        # Clock-based move revalidation (reconstruct's clock_by_side) is off:
        # its premise - a real move always costs the mover clock time - is
        # false for every game's first move per side (lichess and chess.com
        # clocks only start after it) and for premoves. Fed the *true*
        # clocks of a real lichess game (lAniuIChYuA vs lichess.org/jnY8B9ca)
        # it rejected the game entirely; it only looked safe while one side's
        # clock usually went undetected.

        for side, readings in clock_readings.items():
            movers = [(i % 2 == 0) == (side == "white") for i in range(len(g.move_seconds))]
            values = clock_read.clock_at_moves(readings, g.move_seconds, movers)
            if side == "white":
                g.clock_white_seconds = values
            else:
                g.clock_black_seconds = values


def annotate_ratings(src: download.Source, box, games: list[reconstruct.Game]) -> None:
    """Best-effort: read each game's two player ratings from a single high-
    resolution frame a few seconds into the game (see download.fetch_hires_frame
    and rating_read.py). Unlike clocks, a rating is static for the whole
    game, so one frame is all this ever needs - no per-move sampling.

    Skipped entirely for a local-file source (no higher-resolution version
    to fetch than what's already on disk) or when the regular scan's board
    box came from a differently-sized frame than the hi-res one - the box
    is rescaled by the two frames' height ratio, which only makes sense
    when both are views of the same video.
    """
    if not download._looks_like_url(src.url):
        return
    cap = cv2.VideoCapture(str(src.path))
    lowres_h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    cap.release()
    if not lowres_h:
        return

    for g in games:
        if not g.move_seconds:
            continue
        # Try a few candidate timestamps, stopping as soon as both sides
        # are read - most games resolve on the first (cheapest) one, so
        # this doesn't meaningfully slow down the common case. Confirmed
        # on a real video: right after a new game starts, the broadcast's
        # opponent-info panel can still show a placeholder ("Opponent",
        # blank avatar, no rating) for several seconds before matchmaking
        # populates the real name/rating - a fixed single early timestamp
        # (start+8s) caught this transient placeholder state outright on a
        # short game, even though the real info was on screen just 12s
        # later, well within the same game's own window.
        duration = max(0.0, g.move_seconds[-1] - g.start_seconds)
        candidates = [
            min(g.start_seconds + 8.0, g.move_seconds[-1]),
            min(g.start_seconds + max(20.0, duration * 0.5), g.move_seconds[-1]),
            g.move_seconds[-1],
        ]
        seen: list[float] = []
        for t in candidates:
            if not any(abs(t - s) < 1.0 for s in seen):
                seen.append(t)

        # Track the best (highest-confidence) reading per side across
        # attempts, not just the first non-None one - confirmed on a real
        # video, an earlier frame's read for the very same, unchanging
        # rating can classify a digit slightly worse than a later frame's
        # (ordinary per-frame compression/rendering noise), so "first
        # success" can lock in a worse answer than one already sitting in
        # a later candidate. Stop early once both sides clear a "good
        # enough" bar rather than always spending every candidate's
        # network fetch - most games resolve well on the first, cheapest
        # timestamp.
        #
        # Two phases, not one: phase 1 tries every candidate timestamp
        # with ONLY the direct above/below-board search - a phase-1 miss
        # on one frame (e.g. a transient "loading" placeholder before
        # matchmaking populates a name) is exactly what the later
        # candidates are for. Only if a side is *still* missing after every
        # timestamp's direct search does phase 2 allow the wider sidebar-
        # style fallback search, and only on frames already fetched in
        # phase 1 (no extra network cost) - confirmed on a real video, that
        # fallback's much wider search area can confidently misread a
        # channel's own static "N wins - N losses" title card as a rating,
        # and if it's allowed to fire on the very first (transient-failure)
        # frame, that high-confidence wrong answer blocks phase 1 from ever
        # reaching a later frame that would have read correctly.
        # Phase 1 votes rather than keeping the single most confident read:
        # the band search is sensitive to a few pixels of board-box offset
        # (confirmed on a real video: at the calibrated box "2122", 6px up
        # "199"; the other side "2201" / "2241" / nothing), so each frame
        # is read at small vertical shifts and the value most reads agree on
        # wins, confidence breaking ties. A read from the row beside a clock
        # doesn't depend on the board box and counts double. Stops early
        # once every shift of a frame agrees on both sides.
        votes = {"white": {}, "black": {}}   # value -> [count, summed confidence]

        def vote(side, value, conf, weight=1):
            if value is None:
                return
            v = votes[side].setdefault(value, [0, 0.0])
            v[0] += weight
            v[1] += conf or 0.0

        def leader(side):
            if not votes[side]:
                return None, 0
            value, (count, _) = max(votes[side].items(), key=lambda kv: (kv[1][0], kv[1][1]))
            return value, count

        frames_and_boxes: list[tuple] = []
        for ts in seen:
            frame = download.fetch_hires_frame(src.url, ts)
            if frame is None:
                continue
            scale = frame.shape[0] / lowres_h
            scaled_box = (round(box.x * scale), round(box.y * scale),
                          round(box.w * scale), round(box.h * scale))
            frames_and_boxes.append((frame, scaled_box))
            # same top/bottom -> white/black convention as
            # clock_read.side_for_box: a broadcast always shows each
            # player's own info on their own side.
            top_side, bottom_side = ("white", "black") if g.flipped else ("black", "white")
            for dy in RATING_BOX_SHIFTS:
                shifted = (scaled_box[0], scaled_box[1] + round(dy * scale), scaled_box[2], scaled_box[3])
                reading = rating_read.read_ratings(frame, shifted, allow_region_fallback=False)
                vote(top_side, reading.top, reading.top_confidence)
                vote(bottom_side, reading.bottom, reading.bottom_confidence)
            if len(g.clock_boxes) == 2:
                # On lichess the band above/below the board holds nothing
                # useful, or the info panel naming both players; the row
                # beside each clock is the real source there.
                upper, lower = ([round(v * scale) for v in b] for b in g.clock_boxes)
                beside = rating_read.read_ratings_beside_clocks(frame, scaled_box, upper, lower)
                vote(top_side, beside.top, beside.top_confidence, weight=2)
                vote(bottom_side, beside.bottom, beside.bottom_confidence, weight=2)
            if all(leader(side)[1] >= len(RATING_BOX_SHIFTS) for side in votes):
                break
        white, black = leader("white")[0], leader("black")[0]
        white_conf = black_conf = -1.0

        if white is None or black is None:
            for frame, scaled_box in frames_and_boxes:
                reading = rating_read.read_ratings(frame, scaled_box, allow_region_fallback=True)
                if g.flipped:
                    cand_white, cand_white_conf = reading.top, reading.top_confidence
                    cand_black, cand_black_conf = reading.bottom, reading.bottom_confidence
                else:
                    cand_white, cand_white_conf = reading.bottom, reading.bottom_confidence
                    cand_black, cand_black_conf = reading.top, reading.top_confidence
                if white is None and cand_white is not None and cand_white_conf > white_conf:
                    white, white_conf = cand_white, cand_white_conf
                if black is None and cand_black is not None and cand_black_conf > black_conf:
                    black, black_conf = cand_black, cand_black_conf

        # Same floor for every reading path (see rating_read._ocr_digits).
        white = white if white is not None and white >= 100 else None
        black = black if black is not None and black >= 100 else None
        g.rating_white, g.rating_black = rating_read.drop_implausible_pairing(white, black)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    print(f"[1/6] resolving {args.target}")
    src = download.fetch(args.target, max_height=args.max_height)
    print(f"      {src.title!r} - {src.channel or 'unknown channel'}  ({src.path.name})")

    print("[2/6] locating board")
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

    saved = download.CACHE / f"{src.video_id}-readings.json"
    if args.from_readings and saved.exists() and not args.start and not args.end:
        print(f"[3/6] reusing the readings in {saved.name}")
        data = json.loads(saved.read_text())
        readings = [(ts, pl) for ts, pl in data["readings"]]
        # the piece templates aren't saved: calibrate them again on the first opening frame
        model = None
        for ts, frame in iter_frames(src.path, args.fps, 0.0, 0.0):
            if recognize.looks_like_start(recognize.occupancy(frame, box)):
                model, _ = recognize.calibrate_from_start(frame, box)
                if model is not None:
                    break
        box = board_detect.Box(*data["box"], score=1.0)
    else:
        print(f"[3/6] scanning frames @ {args.fps} fps")
        model, box, readings = read_frames(iter_frames(src.path, args.fps, args.start, args.end), box, log=print)
    if model is None:
        print("      never saw an opening position - no games recorded", file=sys.stderr)
        return 3
    if not args.from_readings and not args.start and not args.end and download._looks_like_url(src.url):
        # keep the raw readings: reconstruction can then be re-run or traced
        # (tests/trace_video.py) without reading the frames again
        (download.CACHE / f"{src.video_id}-readings.json").write_text(
            json.dumps({"box": box.as_tuple(), "fps": args.fps, "readings": readings}))
    print(f"      {len(readings)} frames, {sum(1 for _, p in readings if p)} with a readable board")

    print("[4/6] reconstructing games")
    readings = recognize.vote_stream(readings)
    games = reconstruct.reconstruct(readings)
    kept = [g for g in games if g.confidence >= args.min_confidence and g.ply >= args.min_ply]
    if not args.no_refine:
        split = sum(refine.refine_game(src.path, box, model, g, iter_frames) for g in kept)
        print(f"      re-read fast-move moments at {refine.DENSE_FPS} fps: {split} plies seen on their own")
    print(f"      {len(games)} games found, {len(kept)} kept "
          f"(confidence >= {args.min_confidence}, >= {args.min_ply} plies)")

    if args.no_clocks:
        print("[5/6] reading clocks - skipped (--no-clocks)")
    else:
        print("[5/6] reading clocks")
        annotate_clocks(src.path, box, kept)
        n_clocked = sum(1 for g in kept if g.clock_white_seconds or g.clock_black_seconds)
        print(f"      clock data for {n_clocked}/{len(kept)} games")

    if args.no_ratings:
        print("[6/6] reading player ratings - skipped (--no-ratings)")
    else:
        print("[6/6] reading player ratings")
        annotate_ratings(src, box, kept)
        n_rated = sum(1 for g in kept if g.rating_white is not None or g.rating_black is not None)
        print(f"      rating data for {n_rated}/{len(kept)} games")

    for i, g in enumerate(kept):
        flip = " [flipped]" if g.flipped else ""
        clocks = ""
        if g.clock_white_seconds or g.clock_black_seconds:
            w = fmt_clock(g.clock_white_seconds[-1]) if g.clock_white_seconds else "?"
            b = fmt_clock(g.clock_black_seconds[-1]) if g.clock_black_seconds else "?"
            clocks = f"  clocks(end) W={w} B={b}"
        ratings = ""
        if g.rating_white is not None or g.rating_black is not None:
            ratings = f"  ratings W={g.rating_white} B={g.rating_black}"
        pov = "black" if g.flipped else "white"
        print(f"\n  game {i + 1}: {g.ply} plies  conf={g.confidence:.2f}  "
              f"t={int(g.start_seconds)}s{flip}  narrator={pov}{clocks}{ratings}")
        print(f"    {g.pgn()[:240]}")

    if args.dry_run:
        print("\n(dry run - games.json not modified)")
        return 0

    added, total = catalog.merge(src, kept, args.catalog, speedrun=args.speedrun)
    print(f"\nwrote {args.catalog}  (+{added} for this video, {total} total)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
