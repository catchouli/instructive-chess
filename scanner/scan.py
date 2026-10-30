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
import dataclasses
import hashlib
import os
import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import chess
import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board_detect          # noqa: E402
import catalog               # noqa: E402
import clock_read            # noqa: E402
import download              # noqa: E402
import game_end              # noqa: E402
import rating_read           # noqa: E402
import recognize             # noqa: E402
import reconstruct           # noqa: E402
import refine                # noqa: E402
import transcript            # noqa: E402

RECALIBRATE_ON_REFIT = os.environ.get("RECALIBRATE_ON_REFIT", "1") == "1"  # re-cut the piece templates when a re-fit moves the box (recognize.recalibrate)
CLOCK_MIN_COVERAGE = 0.35  # a candidate clock region must read at least this
                           # fraction of its window's frames to be trusted - an
                           # idle clock reads poorly in some skins, and misreads
                           # are blanked by clock_read._physically_consistent
CLOCK_MAX_TICK_SHARE = 0.5  # a player clock never counts down non-stop for more of the game than this
CLOCK_MAX_CANDIDATES = 4  # candidate regions screened per game; the best-reading two are kept
CLOCK_SCREEN_FRAMES = 40  # frames each candidate is screened on before a full read
RATING_BOX_SHIFTS = (-3, 0, 3)  # board-box offsets (scan pixels) each rating frame is read at
RETUNE_MIN_ERR = 4         # re-fit the board box only when the opening reads this many
                           # squares off: a board that really moved read 12 off; at 1-2
                           # (a highlight, the cursor) re-fitting moved a good box to a
                           # worse one and cost two games 60 and 25 correct plies
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
    p.add_argument("--no-stall-reread", action="store_true",
                   help="skip re-reading at a higher frame rate the stretches where a game was lost (see refine.py)")
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


def code_stamp(*modules) -> str:
    """Fingerprint of the modules a cached reading came from: a cache made by
    other code is read again, never reused."""
    h = hashlib.sha256()
    for m in modules:
        h.update(Path(m.__file__).read_bytes())
    return h.hexdigest()[:16]


# How a cached model was fitted. Until 2026-10-27 a --from-readings rescan cut
# the templates at the box calibrate_from_start tuned, then read the 6 fps
# frames with the readings' box, a few pixels off in nearly every video: on
# M_iIF9efRgQ half the white pieces were never seen, and in 76 videos the 6 fps
# frames matched the 1 fps readings on under 10% of moments. Now the model is
# kept with the box it was cut at, chosen among the first opening frames by
# how well it reads the video's 1 fps readings back. Older models aren't reused.
MODEL_FIT = "tuned box, checked against the readings"
FIT_WINDOW_S = 600.0   # opening frames are looked for in the first ten minutes first
FIT_TRIES = 5          # ...and up to this many are fitted and compared
FIT_CHECK = 30         # on about this many frames the saved readings read


def model_stamp(model: "recognize.BoardModel | None") -> str:
    """Fingerprint of a model's templates, for caches of what it read."""
    if model is None:
        return "none"
    h = hashlib.sha256()
    for a in (model.temps, model.masks, model.empty):
        if a is not None:
            h.update(np.ascontiguousarray(a).tobytes())
    h.update(json.dumps([str(getattr(model, k)) for k in ("piece_of", "flipped", "occ_thresh", "occ_diff",
                                                           "match_thresh", "n_base")]).encode())
    return h.hexdigest()[:16]


def _model_file(video_id: str, suffix: str = "") -> Path:
    return download.CACHE / f"{video_id}-model{suffix}.npz"


def load_model(video_id: str, readings_box, suffix: str = "") -> "tuple[recognize.BoardModel, board_detect.Box] | tuple[None, None]":
    """The piece templates a --from-readings run calibrated before for these
    readings, if the board and template code is unchanged: finding them means
    decoding the video up to its first opening position and locating the
    board again (most of a cached rescan's time)."""
    f = _model_file(video_id, suffix)
    if not f.exists():
        return None, None
    with np.load(f, allow_pickle=False) as z:
        meta = json.loads(str(z["meta"]))
        if meta.get("code") != code_stamp(recognize, board_detect) or meta.get("box") != list(readings_box) \
                or meta.get("fit") != MODEL_FIT:
            return None, None
        model = recognize.BoardModel(temps=z["temps"], masks=z["masks"], empty=z["empty"] if "empty" in z else None,
                                     **{k: v for k, v in meta["fields"].items()})
        return model, board_detect.Box(*meta["read_box"], score=1.0)


def save_model(video_id: str, readings_box, model: "recognize.BoardModel", read_box, suffix: str = "") -> None:
    fields = {k: getattr(model, k) for k in ("piece_of", "flipped", "occ_thresh", "occ_diff", "match_thresh", "n_base")}
    fields = {k: (list(v) if k == "piece_of" else v.item() if hasattr(v, "item") else v) for k, v in fields.items()}
    arrays = {"temps": model.temps, "masks": model.masks}
    if model.empty is not None:
        arrays["empty"] = model.empty
    meta = {"code": code_stamp(recognize, board_detect), "box": list(readings_box), "fit": MODEL_FIT,
            "read_box": [float(v) for v in read_box.as_tuple()], "fields": fields}
    f = _model_file(video_id, suffix)
    tmp = f.with_name(f.stem + ".tmp.npz")
    np.savez(tmp, meta=json.dumps(meta), **arrays)
    os.replace(tmp, f)


def fit_model_to_readings(path: Path, boxes, readings: list[tuple[float, str | None]], log=lambda msg: None,
                          span: tuple[float, float] = (0.0, 0.0)):
    """Piece templates for reading this video's frames the way its saved
    readings were read: fitted (recognize.calibrate_from_start, which also
    tunes the box) on each of the first FIT_TRIES opening frames, and scored
    by how many of ~FIT_CHECK sampled frames they read the same as the saved
    readings. Looks in the first FIT_WINDOW_S seconds, then the whole video,
    with each box of `boxes` in turn (the readings' box first: on three videos
    only the box found again in the video showed an opening position).
    `span` limits it to one stretch of the video (end 0 = the video's end).
    Returns (model, the box it reads with) or (None, None)."""
    saved = {round(t, 3): p for t, p in readings}
    s0, s1 = span
    # the video's end, so no window starts past it (ffmpeg fails on one: a stretch starting
    # under FIT_WINDOW_S before the end failed 23 videos)
    s1 = s1 or (readings[-1][0] + 1.0 if readings else 0.0)
    windows = [(s0, s0 + FIT_WINDOW_S if not s1 else min(s1, s0 + FIT_WINDOW_S))]
    if not s1 or s1 > s0 + FIT_WINDOW_S:
        windows.append((s0 + FIT_WINDOW_S, s1))
    for box, (a, b) in ((box, w) for box in boxes for w in windows):
        fits, check = [], []
        every = max(1, int(((b or (readings[-1][0] if readings else a)) - a) / FIT_CHECK))
        for i, (ts, frame) in enumerate(iter_frames(path, 1.0, a, b)):
            if saved.get(round(ts, 3)) and i % every == 0:
                check.append((ts, frame))
            if len(fits) < FIT_TRIES and recognize.looks_like_start(recognize.occupancy(frame, box)):
                model, tuned = recognize.calibrate_from_start(frame, box)
                if model is not None:
                    fits.append((ts, model, tuned))
        if not fits:
            continue

        def score(fit):
            _, model, tuned = fit
            return sum(recognize.read_board(f, tuned, model).placement == saved[round(ts, 3)] for ts, f in check)
        scored = [(score(fit), fit) for fit in fits]
        n, (ts, model, tuned) = max(scored, key=lambda x: x[0])
        log(f"      templates fitted at t={int(ts)}s, box {tuned.x},{tuned.y} {tuned.w}x{tuned.h} (read {n} of "
            f"{len(check)} sampled frames as saved; other fits {sorted(x for x, _ in scored)[:-1]})")
        return model, tuned
    return None, None


def _by_part(parts: list[dict], games: list) -> list[tuple[dict, list]]:
    """Each stretch of the video (see tests/relocate.py) with the games that start in it."""
    out = []
    for i, p in enumerate(parts):
        t1 = parts[i + 1]["t0"] if i + 1 < len(parts) else math.inf
        out.append((p, [g for g in games if p["t0"] <= g.start_seconds < t1]))
    return [(p, gs) for p, gs in out if gs]


def write_json(path: Path, data) -> None:
    """Temp file + rename, so an interrupted write can't leave a cut-off cache."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def iter_frames(path: Path, fps: float, start: float, end: float):
    """Yield (timestamp, frame_bgr) at ~fps. ffmpeg decodes and downsamples the
    whole video in one pass - far faster than per-sample seeking in OpenCV."""
    tmp = Path(tempfile.mkdtemp(prefix="instructive-chess-"))
    try:
        cmd = ["ffmpeg", "-nostdin", "-v", "error"]
        # ffmpeg decodes on ~5 cores by default; parallel jobs cap it so
        # their total stays within the machine's cores
        if os.environ.get("FFMPEG_THREADS"):
            cmd += ["-threads", os.environ["FFMPEG_THREADS"]]
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


def read_frames(frames, box, log=lambda msg: None, model: "recognize.BoardModel | None" = None):
    """Read every (timestamp, frame) into a (timestamp, placement-or-None)
    stream: self-calibrate piece templates at the first opening position
    (see recognize.calibrate_from_start), re-fit the board box at every
    later one (recognize.retune_at_start - streams shift the board between
    games), then match each frame. Returns (model, box, readings); model is
    None if no opening position was ever seen. Shared by scan.py and
    tests/dump_readings.py so they can't drift apart. Given a `model` (and
    the box it was cut at), reads with it from the first frame."""
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
        if start_err >= RETUNE_MIN_ERR and recognize.looks_like_start_reading(rd.placement) \
                and ts - retune_failed_at > 60:
            # (reading first and testing the result costs nothing on the
            # ordinary frame, unlike an occupancy pre-check of every frame;
            # the re-fit itself is slow, so a failed one isn't retried for
            # a minute - the next game brings its own start position)
            retuned = recognize.retune_at_start(frame, box, model, known_err=start_err)
            if retuned.as_tuple() != box.as_tuple():
                log(f"      board moved: box {box.x},{box.y} -> {retuned.x},{retuned.y} "
                    f"{retuned.w}x{retuned.h} at t={int(ts)}s")
                box = retuned
                if RECALIBRATE_ON_REFIT:
                    fresh = recognize.recalibrate(frame, box, model)
                    if fresh is not None:
                        log(f"      templates re-cut at t={int(ts)}s")
                        model = fresh
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


def annotate_clocks(path: Path, board_box, games: list[reconstruct.Game], cache: dict | None = None) -> None:
    """Best-effort: read each game's own on-screen clock(s) over just that
    game's video window - bounded memory regardless of how long the source
    video is, unlike decoding the whole thing into clock_read at once.

    Leaves clock_*_seconds at their empty default (see reconstruct.Game) for
    any game this can't read a clock confidently for: a missing clock
    reading next to a real move is far less harmful than a wrong one, and
    clock_read.py is already deliberately conservative about reporting a
    value at all (see its module docstring).

    cache (scanner/cache/<id>-clocks.json, kept by main) holds what was read
    before - the clock regions and screening reads of each window, and each
    region's raw readings from each window start - so a rerun decodes and
    reads only the windows that changed, with the same result.
    """
    cache = {} if cache is None else cache
    for k in ("times", "regions", "early", "screen", "full"):
        cache.setdefault(k, {})
    for g in games:
        if not g.move_seconds:
            continue
        # on whole seconds, so a game that ends a ply later shares a window start
        start = float(max(0, math.floor(g.start_seconds - 1.0)))
        end = float(math.ceil(g.move_seconds[-1] + 3.0))
        win = f"{start:.0f}-{end:.0f}"
        decoded: list = []

        def window():
            if not decoded:
                fr, tm = [], []
                for ts, frame in iter_frames(path, 1.0, start, end):
                    fr.append(frame)
                    tm.append(ts)
                decoded.append(fr)
                cache["times"][win] = tm
            return decoded[0]

        if win not in cache["times"]:
            window()
        times = cache["times"][win]
        if len(times) < CLOCK_MIN_FRAMES:
            continue

        def boxes(rows):
            return [clock_read.ClockBox(*r) for r in rows]

        def rows(cboxes):
            return [[int(c.x), int(c.y), int(c.w), int(c.h)] for c in cboxes]

        # Screen every candidate on a sample of frames, then read only the
        # chosen clocks in full - OCR per frame is the slow part.
        step = max(1, len(times) // CLOCK_SCREEN_FRAMES)
        # Over a long game other changing UI near a clock (a chat, a stream
        # timer) can merge with it into one wide blob that reads nothing
        # (confirmed on a stream: the top clock was found over the first
        # four minutes and lost over the whole game). Pool the candidates
        # from the whole window and from its opening minutes.
        if win not in cache["regions"]:
            cache["regions"][win] = rows(clock_read.find_clock_regions(window(), board_box,
                                                                       max_regions=CLOCK_MAX_CANDIDATES))
        cands = boxes(cache["regions"][win])
        if len(times) > CLOCK_EARLY_FRAMES:
            early = f"{start:.0f}"
            if early not in cache["early"]:
                cache["early"][early] = rows(clock_read.find_clock_regions(window()[:CLOCK_EARLY_FRAMES], board_box,
                                                                           max_regions=CLOCK_MAX_CANDIDATES))
            for cbox in boxes(cache["early"][early]):
                if not any(_overlaps(cbox, c) for c in cands):
                    cands.append(cbox)

        def screen(cbox):
            key = f"{cbox.x},{cbox.y},{cbox.w},{cbox.h}@{win}"
            if key not in cache["screen"]:
                rd = clock_read.read_clock_ocr(window()[::step], times[::step], cbox, reject_outliers=False)
                cache["screen"][key] = [[r.t, r.seconds] for r in rd]
            return [clock_read.ClockReading(t, v) for t, v in cache["screen"][key]]

        def full(cbox):
            # a region's raw readings from a window start; a shorter window's are their first part
            key = f"{cbox.x},{cbox.y},{cbox.w},{cbox.h}@{start:.0f}"
            got = cache["full"].get(key, [])
            if [t for t, _ in got[:len(times)]] != times:
                got = [[r.t, r.seconds] for r in clock_read.read_clock_ocr_raw(window(), times, cbox)]
                if len(got) > len(cache["full"].get(key, [])):
                    cache["full"][key] = got
            return clock_read.clean_readings([clock_read.ClockReading(t, v) for t, v in got[:len(times)]])

        screened = [(cbox, screen(cbox)) for cbox in cands]
        box_for = {id(rd): cbox for cbox, rd in screened}
        full_for: dict[int, list] = {}
        while True:
            chosen = clock_read.assign_clock_sides(screened, board_box, g.flipped, CLOCK_MIN_COVERAGE)
            for rd in chosen.values():
                if id(rd) not in full_for:
                    full_for[id(rd)] = full(box_for[id(rd)])
            # A player's clock stops on every opponent move; a countdown
            # that runs for most of the game is something else (see
            # clock_read.longest_tick_share).
            ticking = [rd for rd in chosen.values()
                       if clock_read.longest_tick_share(full_for[id(rd)]) > CLOCK_MAX_TICK_SHARE]
            if not ticking:
                break
            screened = [(c, rd) for c, rd in screened if all(rd is not t for t in ticking)]
        if len(chosen) == 2:
            g.clock_boxes = sorted(((int(c.x), int(c.y), int(c.w), int(c.h))
                                    for c in (box_for[id(rd)] for rd in chosen.values())),
                                   key=lambda b: b[1])
        clock_readings = {}
        for side, rd in chosen.items():
            full = full_for[id(rd)]
            if sum(r.seconds is not None for r in full) >= CLOCK_MIN_COVERAGE * len(full):
                clock_readings[side] = full
        if not clock_readings:
            continue

        keep = game_end.live_move_count(g.move_seconds, clock_readings,
                                          lambda n: _game_over_after(g.moves_san, n))
        if keep < len(g.moves_san):
            print(f"      t={int(g.start_seconds)}s: clocks never ran after: {' '.join(g.moves_san[keep:])}")
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


def annotate_ratings(src: download.Source, box, games: list[reconstruct.Game], cache: dict | None = None) -> None:
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

    # cache (scanner/cache/<id>-ratings.json, kept by main): each hi-res
    # frame's height and every read made of it, so a rerun reads none again
    cache = {} if cache is None else cache
    heights, reads = cache.setdefault("heights", {}), cache.setdefault("reads", {})
    loaded: dict[str, np.ndarray | None] = {}

    def frame_at(ts):
        k = f"{ts:.2f}"
        if k not in loaded:
            loaded[k] = download.fetch_hires_frame(src.url, ts)
            heights[k] = None if loaded[k] is None else int(loaded[k].shape[0])
        return loaded[k]

    def read(fn, ts, *args, **kw):
        k = f"{fn.__name__}|{ts:.2f}|{json.dumps([args, kw])}"
        if k not in reads:
            reads[k] = {f: (v.item() if hasattr(v, "item") else v)
                        for f, v in dataclasses.asdict(fn(frame_at(ts), *args, **kw)).items()}
        return rating_read.RatingReading(**reads[k])

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
            # on whole seconds: the fetched frames are cached by timestamp, and a
            # move time read at 6 fps would otherwise fetch a new one on every rescan
            t = float(round(t))
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
            if f"{ts:.2f}" not in heights:
                frame_at(ts)
            if heights[f"{ts:.2f}"] is None:
                continue
            scale = heights[f"{ts:.2f}"] / lowres_h
            scaled_box = (round(box.x * scale), round(box.y * scale),
                          round(box.w * scale), round(box.h * scale))
            frames_and_boxes.append((ts, scaled_box))
            # same top/bottom -> white/black convention as
            # clock_read.side_for_box: a broadcast always shows each
            # player's own info on their own side.
            top_side, bottom_side = ("white", "black") if g.flipped else ("black", "white")
            for dy in RATING_BOX_SHIFTS:
                shifted = (scaled_box[0], scaled_box[1] + round(dy * scale), scaled_box[2], scaled_box[3])
                reading = read(rating_read.read_ratings, ts, shifted, allow_region_fallback=False)
                vote(top_side, reading.top, reading.top_confidence)
                vote(bottom_side, reading.bottom, reading.bottom_confidence)
            if len(g.clock_boxes) == 2:
                # On lichess the band above/below the board holds nothing
                # useful, or the info panel naming both players; the row
                # beside each clock is the real source there.
                upper, lower = ([round(v * scale) for v in b] for b in g.clock_boxes)
                beside = read(rating_read.read_ratings_beside_clocks, ts, scaled_box, upper, lower)
                vote(top_side, beside.top, beside.top_confidence, weight=2)
                vote(bottom_side, beside.bottom, beside.bottom_confidence, weight=2)
            if all(leader(side)[1] >= len(RATING_BOX_SHIFTS) for side in votes):
                break
        white, black = leader("white")[0], leader("black")[0]
        white_conf = black_conf = -1.0

        if white is None or black is None:
            for ts, scaled_box in frames_and_boxes:
                reading = read(rating_read.read_ratings, ts, scaled_box, allow_region_fallback=True)
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


def _any_kept(model, readings, args) -> bool:
    """Would these readings give at least one game worth keeping?"""
    if model is None:
        return False
    games = reconstruct.reconstruct(recognize.vote_stream(readings), raw=dict(readings))
    return any(g.confidence >= args.min_confidence and g.ply >= args.min_ply for g in games)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    print(f"[1/6] resolving {args.target}")
    src = download.fetch(args.target, max_height=args.max_height)
    print(f"      {src.title!r} - {src.channel or 'unknown channel'}  ({src.path.name})")
    if download._looks_like_url(args.target):
        # best effort: the narrator's commentary, kept timed (see transcript.py)
        try:
            saved = transcript.save(src.video_id)
            print(f"      transcript: {saved.relative_to(transcript.ROOT) if saved else 'none'}")
        except Exception as e:  # noqa: BLE001
            print(f"      transcript: failed ({e!s:.80})")

    print("[2/6] locating board")
    saved = download.CACHE / f"{src.video_id}-readings.json"
    reuse = args.from_readings and saved.exists() and not args.start and not args.end
    data = json.loads(saved.read_text()) if reuse else None
    model, read_box = load_model(src.video_id, data["box"]) if reuse and not args.crop and not args.calibrate else (None, None)
    if model is not None:
        box = board_detect.Box(*data["box"], score=1.0)
        print("      as the saved readings (piece templates cached)")
    elif args.crop:
        x, y, w, h = (int(v) for v in args.crop.split(","))
        box = board_detect.Box(x, y, w, h, score=1.0)
    else:
        other_boxes: list[board_detect.Box] = []
        box = board_detect.detect(sample_frames(src.path, args.start, args.end, 15), others=other_boxes)
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

    if reuse:
        print(f"[3/6] reusing the readings in {saved.name}")
        readings = [(ts, pl) for ts, pl in data["readings"]]
        recognize.TUNE_GRID = data.get("tune_grid", 0.0)    # fit the box as the scan that saved them did
        found, box = box, board_detect.Box(*data["box"], score=1.0)
        # a later entry at the same time replaces an earlier one (no empty stretches)
        segs = [g for i, g in enumerate(data.get("segments", []))
                if not any(h["t"] == g["t"] for h in data["segments"][i + 1:])]
        if model is None:
            # fit the piece templates again, as the readings were read (see MODEL_FIT), and keep them
            model, read_box = fit_model_to_readings(src.path, [box, found], readings, log=print,
                                                    span=(0.0, segs[0]["t"] if segs else 0.0))
            if model is not None and not args.crop:
                save_model(src.video_id, data["box"], model, read_box)
        # where the board moved mid-stream (tests/relocate.py), each stretch has its own box and templates
        parts = [{"t0": 0.0, "box": box, "model": model, "read_box": read_box, "suffix": ""}]
        for i, seg in enumerate(segs, 1):
            sbox = board_detect.Box(*seg["box"], score=1.0)
            t1 = segs[i]["t"] if i < len(segs) else 0.0
            m, rb = load_model(src.video_id, seg["box"], suffix=f"-{i}")
            if m is None:
                print(f"      from t={int(seg['t'])}s the board is at {sbox.x},{sbox.y} {sbox.w}x{sbox.h}")
                m, rb = fit_model_to_readings(src.path, [sbox], [r for r in readings if r[0] >= seg["t"]],
                                              log=print, span=(seg["t"], t1))
                if m is None:
                    # most such stretches go back to a board read before (relocate.py marks where the
                    # video's own box is in use again after a stretch): its templates read them (198
                    # of 1,034 stretches, 17 h of video, had none - no 6 fps frames, clocks or ratings)
                    m, rb = next(((p["model"], p["read_box"]) for p in reversed(parts)
                                  if p["model"] is not None and list(p["box"].as_tuple()) == list(seg["box"])), (None, None))
                if m is not None:
                    save_model(src.video_id, seg["box"], m, rb, suffix=f"-{i}")
            parts.append({"t0": seg["t"], "box": sbox, "model": m, "read_box": rb, "suffix": f"-{i}"})

    else:
        print(f"[3/6] scanning frames @ {args.fps} fps")
        model, box_read, readings = read_frames(iter_frames(src.path, args.fps, args.start, args.end), box, log=print)
        if model is None and not args.crop:
            # the most-voted board never showed an opening position: try
            # the video's other boards (see board_detect.detect)
            for alt in other_boxes[:2]:
                print(f"      no opening position there; trying the board at x={alt.x} y={alt.y} {alt.w}x{alt.h}")
                model, box_read, readings = read_frames(iter_frames(src.path, args.fps, args.start, args.end), alt, log=print)
                if model is not None:
                    break
        if not args.crop and not _any_kept(model, readings, args):
            # nothing came of it: fit the box by the squares of the opening's
            # empty middle ranks instead of by its pieces (recognize.TUNE_GRID).
            # Only as a fallback: on 22 videos that already gave games it found
            # a worse box as often as a better one (171 games listed -> 149).
            print("      no games; reading again with the box fitted to the board's squares")
            recognize.TUNE_GRID = 1.0
            grid = read_frames(iter_frames(src.path, args.fps, args.start, args.end), box, log=print)
            if _any_kept(grid[0], grid[2], args):
                model, box_read, readings = grid
            else:
                recognize.TUNE_GRID = 0.0
        box = box_read
        parts = [{"t0": 0.0, "box": box, "model": model, "read_box": None, "suffix": ""}]
    if model is None:
        print("      never saw an opening position - no games recorded", file=sys.stderr)
        return 3
    if not args.from_readings and not args.start and not args.end and download._looks_like_url(src.url):
        # keep the raw readings: reconstruction can then be re-run or traced
        # (tests/trace_video.py) without reading the frames again
        # temp file + rename, so an interrupted write can't leave a cut-off cache
        cached = download.CACHE / f"{src.video_id}-readings.json"
        tmp = cached.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"box": box.as_tuple(), "fps": args.fps, "tune_grid": recognize.TUNE_GRID,
                                   "readings": readings}))
        os.replace(tmp, cached)
    print(f"      {len(readings)} frames, {sum(1 for _, p in readings if p)} with a readable board")

    # 6 fps frames, shared by the stall and fast-move re-reads and kept (for
    # a whole video), so a --from-readings run decodes only frames it hasn't read
    whole = not args.start and not args.end
    # the 6 fps frames are read with the box the model was cut at (see MODEL_FIT), per stretch
    dense_files = []
    for p in parts:
        f = download.CACHE / f"{src.video_id}-dense{p['suffix']}.json"
        frames = None if p["model"] is None else refine.DenseFrames(
            src.path, p["read_box"] or p["box"], p["model"], iter_frames,
            stamp=f"{code_stamp(recognize)}-{model_stamp(p['model'])}",
            cache=json.loads(f.read_text()) if whole and f.exists() else None)
        dense_files.append((f, frames))
    dense = refine.SegmentedFrames([(p["t0"], frames) for p, (_, frames) in zip(parts, dense_files)], readings)
    dense_ok = True
    if refine.DENSE_MIN_AGREE and whole:
        agree = dense.agreement(readings)
        dense_ok = agree is None or agree >= refine.DENSE_MIN_AGREE
        if not dense_ok:
            print(f"      6 fps frames agree with the readings on {agree:.0%} of moments: not used")
    if not args.no_stall_reread and whole and dense_ok:
        readings = refine.reread_stalls(readings, dense, log=print)

    print("[4/6] reconstructing games")
    raw = dict(readings)
    readings = recognize.vote_stream(readings)
    games = reconstruct.reconstruct(readings, raw=raw)
    kept = [g for g in games if g.confidence >= args.min_confidence and g.ply >= args.min_ply]
    if not args.no_refine and dense_ok:
        split = sum(refine.refine_game(g, dense) for g in kept)
        print(f"      re-read fast-move moments at {refine.DENSE_FPS} fps: {split} plies seen on their own")
    print(f"      {len(games)} games found, {len(kept)} kept "
          f"(confidence >= {args.min_confidence}, >= {args.min_ply} plies)")
    for f, frames in dense_files:
        if whole and frames is not None and frames.added:
            write_json(f, frames.to_json())

    if args.no_clocks:
        print("[5/6] reading clocks - skipped (--no-clocks)")
    else:
        print("[5/6] reading clocks")
        # what each window read is kept, keyed by the board box and the clock code
        for p, part_games in _by_part(parts, kept):
            clocks_file = download.CACHE / f"{src.video_id}-clocks{p['suffix']}.json"
            key = {"box": [int(v) for v in p["box"].as_tuple()], "code": code_stamp(clock_read)}
            clocks = json.loads(clocks_file.read_text()) if whole and clocks_file.exists() else {}
            if any(clocks.get(k) != v for k, v in key.items()):
                clocks = dict(key)
            before = json.dumps(clocks, sort_keys=True)
            annotate_clocks(src.path, p["box"], part_games, cache=clocks)
            if whole and json.dumps(clocks, sort_keys=True) != before:
                write_json(clocks_file, clocks)
        frozen = [g for i, g in enumerate(kept) if reconstruct.is_frozen_replay(g, kept[:i])]
        for g in frozen:
            print(f"      t={int(g.start_seconds)}s: clocks never ran in a replay of an earlier game - dropped")
        kept = [g for g in kept if not any(g is f for f in frozen)]
        n_clocked = sum(1 for g in kept if g.clock_white_seconds or g.clock_black_seconds)
        print(f"      clock data for {n_clocked}/{len(kept)} games")

    if args.no_ratings:
        print("[6/6] reading player ratings - skipped (--no-ratings)")
    else:
        print("[6/6] reading player ratings")
        for p, part_games in _by_part(parts, kept):
            ratings_file = download.CACHE / f"{src.video_id}-ratings{p['suffix']}.json"
            key = {"box": [int(v) for v in p["box"].as_tuple()], "code": code_stamp(rating_read, download)}
            ratings = json.loads(ratings_file.read_text()) if whole and ratings_file.exists() else {}
            if any(ratings.get(k) != v for k, v in key.items()):
                ratings = dict(key)
            before = json.dumps(ratings, sort_keys=True)
            annotate_ratings(src, p["box"], part_games, cache=ratings)
            if whole and json.dumps(ratings, sort_keys=True) != before:
                write_json(ratings_file, ratings)
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
