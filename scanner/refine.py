"""Re-read the moments where several moves were confirmed at once, at a
higher frame rate, to see each move on its own.

At the scan's 1 fps, fast play (openings, premoves, time scrambles) lands
several plies in one reading; reconstruction infers the moves in between,
and those bunches are where games go wrong (see trust.py). Sampling the
whole video faster doesn't work: recognize.vote_stream's flicker vote spans
a fixed number of frames, so it smooths half as much time at 2 fps, and on
the full library more games went wrong than right.

So only each bunch's own short window is re-read, and the dense frames are
used just to time it: the moves stay exactly as reconstructed at 1 fps, and
each in-between position along them that the dense frames show (in order)
gives its ply its own time. The moves never change, so this can't make a
game wrong; a bunch whose positions aren't seen - e.g. because the real
order differed - stays bunched, and trust.py keeps not trusting it.
"""
from __future__ import annotations

import math
import os
import subprocess
from pathlib import Path

import chess

import recognize
import reconstruct

DENSE_FPS = 6          # frames per second inside a bunch's window
MIN_BUNCH = 2          # plies confirmed together that are worth re-reading
STABLE_FRAMES = 2      # a dense placement must repeat this often to count
WINDOW_PAD = 3.0       # seconds either side of a bunch to re-read: its 1 fps time comes
                       # from a +-2 frame vote, so the moves can sit a couple of seconds off
GAP_FILL = 2.0         # decode across a gap this short between frames still to read, rather than seek twice
# The dense frames are only used where they agree with the video's 1 fps
# readings on at least DENSE_MIN_AGREE of the moments both cover (0 = always),
# checked on AGREE_MIN+ moments (sampling up to AGREE_SAMPLE uncached ones).
DENSE_MIN_AGREE = float(os.environ.get("REFINE_DENSE_MIN_AGREE", "0"))
AGREE_MIN = 30
AGREE_SAMPLE = 60


class DenseFrames:
    """One video's board readings at DENSE_FPS, on the fixed grid k / DENSE_FPS
    seconds, read once and kept (scanner/cache/<id>-dense.json): the stall
    re-read and the fast-move re-read share them, and a window that moved a
    little between runs - a game that ends a ply later - decodes only the
    frames it hasn't seen. A cache read with another board box or other
    recognize.py code is ignored.

    A frame's placement is None where it can't be trusted (low confidence or
    an implausible piece count); the raw board orientation is kept, callers
    flip it."""

    def __init__(self, path: Path | None, box, model, iter_frames, cache: dict | None = None, stamp: str = ""):
        self.path, self.box, self.model, self.iter_frames = path, box, model, iter_frames
        key = {"box": [int(v) for v in (box.as_tuple() if hasattr(box, "as_tuple") else box)] if box else None,
               "fps": DENSE_FPS, "code": stamp}
        cache = cache or {}
        self.frames: dict[int, str | None] = (
            {int(k): v for k, v in cache.get("frames", {}).items()} if all(cache.get(k) == v for k, v in key.items()) else {})
        self.key, self.added = key, 0

    def to_json(self) -> dict:
        return {**self.key, "frames": {str(k): v for k, v in sorted(self.frames.items())}}

    def read(self, a: float, b: float) -> list[tuple[float, str | None]]:
        """(timestamp, placement) for every grid frame in [a, b]."""
        ks = range(max(0, math.ceil(a * DENSE_FPS - 1e-6)), math.floor(b * DENSE_FPS + 1e-6) + 1)
        todo = [k for k in ks if k not in self.frames]
        runs: list[list[int]] = []
        for k in todo:
            if runs and k - runs[-1][1] <= GAP_FILL * DENSE_FPS:
                runs[-1][1] = k
            else:
                runs.append([k, k])
        for k0, k1 in runs:
            try:
                # to the next grid frame: ffmpeg fails on a window too short to hold a frame
                for i, (_, frame) in enumerate(self.iter_frames(self.path, DENSE_FPS, k0 / DENSE_FPS, (k1 + 1) / DENSE_FPS)):
                    if k0 + i > k1:
                        break
                    rd = recognize.read_board(frame, self.box, self.model)
                    ok = rd.confidence >= 0.72 and 4 <= rd.n_pieces <= 34
                    self.frames[k0 + i] = rd.placement if ok else None
            except subprocess.CalledProcessError:
                pass                              # nothing decodable there (past the video's end)
            for k in range(k0, k1 + 1):          # past the video's end: nothing to read
                self.frames.setdefault(k, None)
            self.added += k1 - k0 + 1
        return [(round(k / DENSE_FPS, 3), self.frames[k]) for k in ks]

    def covers(self, a: float, b: float) -> bool:
        return all(k in self.frames for k in range(max(0, math.ceil(a * DENSE_FPS - 1e-6)),
                                                     math.floor(b * DENSE_FPS + 1e-6) + 1))

    def agreement(self, readings: list[tuple[float, str | None]], sample: int = AGREE_SAMPLE) -> float | None:
        """Share of the moments where a 1 fps reading read a board that this
        reads the same: the two should be the same frame read the same way.
        A --from-readings rescan reads the dense frames with piece templates
        it fitted again, and on some videos that fit is wrong (M_iIF9efRgQ:
        rank 1 read empty all video long; 76 of 535 videos agreed on under
        10% of frames), so a re-read there would put garbage in place of good
        readings. Reads up to `sample` frames that aren't cached yet. None
        when too few moments compare."""
        read = [(t, p) for t, p in readings if p is not None and abs(t * DENSE_FPS - round(t * DENSE_FPS)) < 1e-6]
        have = [(t, p) for t, p in read if round(t * DENSE_FPS) in self.frames]
        if len(have) < AGREE_MIN:
            step = max(1, len(read) // sample)
            for t, _ in read[::step][:sample]:
                self.read(t, t)
            have = [(t, p) for t, p in read if round(t * DENSE_FPS) in self.frames]
        if len(have) < AGREE_MIN:
            return None
        return sum(self.frames[round(t * DENSE_FPS)] == p for t, p in have) / len(have)


class SegmentedFrames:
    """The 6 fps frames of a video whose board moved mid-stream (see
    tests/relocate.py): one DenseFrames per stretch with its own box and
    templates, each request split at the stretch boundaries. A stretch without
    templates (none could be fitted) gives back its 1 fps readings instead."""

    def __init__(self, parts: list[tuple[float, "DenseFrames | None"]], readings: list[tuple[float, str | None]]):
        self.parts = sorted(parts, key=lambda p: p[0])
        self.fallback = readings

    def _spans(self, a: float, b: float):
        for i, (t0, frames) in enumerate(self.parts):
            t1 = self.parts[i + 1][0] if i + 1 < len(self.parts) else math.inf
            lo, hi = max(a, t0), min(b, t1 - 1e-6)
            if lo <= hi:
                yield lo, hi, frames

    def read(self, a: float, b: float) -> list[tuple[float, str | None]]:
        out: list[tuple[float, str | None]] = []
        for lo, hi, frames in self._spans(a, b):
            out += frames.read(lo, hi) if frames is not None else [r for r in self.fallback if lo <= r[0] <= hi]
        return out

    def covers(self, a: float, b: float) -> bool:
        return all(frames is None or frames.covers(lo, hi) for lo, hi, frames in self._spans(a, b))

    def agreement(self, readings: list[tuple[float, str | None]]) -> float | None:
        """The lowest of the stretches' agreements (see DenseFrames.agreement)."""
        found = [frames.agreement([r for r in readings if lo <= r[0] <= hi])
                 for lo, hi, frames in self._spans(0.0, math.inf) if frames is not None]
        found = [x for x in found if x is not None]
        return min(found) if found else None

    @property
    def added(self) -> int:
        return sum(frames.added for _, frames in self.parts if frames is not None)


def _bunches(move_seconds: list[float]) -> list[tuple[int, int]]:
    """(first ply index, ply count) of each run of equal confirmation times."""
    out, i = [], 0
    while i < len(move_seconds):
        j = i
        while j + 1 < len(move_seconds) and move_seconds[j + 1] == move_seconds[i]:
            j += 1
        out.append((i, j - i + 1))
        i = j + 1
    return out


def split_bunch(path_placements: list[str], dense: list[tuple[float, str]],
                t_end: float) -> list[float] | None:
    """When each position along a bunch's known path was first seen in the
    dense frames - its moves stay exactly as reconstructed; only their
    times are refined. A position counts as seen when STABLE_FRAMES
    consecutive dense frames are within one square of it (a single dense
    frame has no flicker vote, so one misread square is common), at least
    one of them exactly, each closer to it than to any other position on
    the path; positions must appear in path order. None unless every ply
    was seen on its own (see the all-or-nothing note below)."""
    k_seen: dict[int, float] = {}
    run_k, run_start, run_n, run_exact = None, None, 0, False
    last_k = -1
    for t, p in dense + [(None, None)]:
        k, exact = None, False
        if p is not None:
            dists = [reconstruct._hamming(p, q) for q in path_placements]
            best = min(dists)
            if best <= 1 and dists.count(best) == 1:
                k, exact = dists.index(best), best == 0
        if k == run_k and k is not None:
            run_n += 1
            run_exact = run_exact or exact
            continue
        # ...and at least one frame of the run must match exactly: one square
        # off is also what a wrong piece on the right square looks like
        # (a real misread: Ra7 for Qa7), which must not confirm a wrong move
        if run_k is not None and run_n >= STABLE_FRAMES and run_exact and run_k > last_k:
            k_seen[run_k] = run_start
            last_k = run_k
        run_k, run_start, run_n, run_exact = k, t, 1, exact
    times: list[float] = []
    nxt = t_end
    for k in range(len(path_placements) - 1, -1, -1):
        nxt = min(k_seen.get(k, nxt), nxt)
        times.append(nxt)
    times.reverse()
    # All or nothing: a partly split bunch leaves smaller bunches that
    # trust.py would treat as ordinary ones, though they came out of a big,
    # riskier one (confirmed on a real game: a 4-ply bunch split 1+1+2 left
    # a trusted pair holding the wrong move).
    return times if len(set(times)) == len(times) else None


def refine_game(game: reconstruct.Game, frames: DenseFrames) -> int:
    """Split what bunches the dense frames allow; returns how many plies got
    their own confirmation time."""
    improved = 0
    for start, size in reversed(_bunches(game.move_seconds)):   # later edits don't shift earlier ones
        if size < MIN_BUNCH:
            continue
        t_end = game.move_seconds[start]
        t_begin = game.move_seconds[start - 1] if start > 0 else game.start_seconds
        board = chess.Board()
        for san in game.moves_san[:start]:
            board.push_san(san)
        path_placements = []
        for san in game.moves_san[start:start + size]:
            board.push_san(san)
            path_placements.append(reconstruct._placement_of(board))

        dense = [(ts, pl[::-1] if game.flipped else pl)
                 for ts, pl in frames.read(max(0.0, t_begin - WINDOW_PAD), t_end + WINDOW_PAD) if pl]
        times = split_bunch(path_placements, dense, t_end)
        if times is None:
            continue
        # strictly after the previous ply (or the split would merge into its
        # bunch), never after the bunch's own 1 fps confirmation
        game.move_seconds[start:start + size] = [round(min(max(t, t_begin + 0.1), t_end), 2) for t in times]
        improved += len(set(times))
        # Every position along the bunch was just seen, in order and exactly
        # at least once: the moves are no longer a best fit to a frame a
        # square off, nor a guess among routes (another route would pass
        # through different positions) - so trust.py may count them.
        span = set(range(start, start + size))
        game.fuzzy = [i for i in game.fuzzy if i not in span]
        game.ambiguous = [i for i in game.ambiguous if i not in span]
    return improved


STALL_FPS = DENSE_FPS  # frames per second when re-reading a stall (the same grid as the fast-move re-read)
STALL_PRE = 4.0        # seconds before a stalled game's last ply to re-read (its last bunch)
STALL_POST = 25.0      # ...and after it: the misses that closed the game
STALL_ROUNDS = 2       # re-read the stalls a second reconstruction still has


def _stalls(readings: list[tuple[float, str | None]], raw: dict | None = None) -> list[float]:
    """When each game closed on too many misses ended: its last ply's time."""
    out: list[float] = []

    def note(ts, msg):
        if msg.startswith("close too many misses"):
            out.append(ts)
    reconstruct.reconstruct(recognize.vote_stream(readings), trace=note, raw=raw)
    return out


def reread_stalls(readings: list[tuple[float, str | None]], frames: DenseFrames,
                  log=lambda msg: None) -> list[tuple[float, str | None]]:
    """A game closed on too many misses usually went off track on moves played
    faster than the 1 fps readings can follow: each reading is a position a
    ply or two further on, and the flicker vote blends consecutive ones into
    boards that never existed. Read each such stretch again at STALL_FPS and
    put those frames in place of the 1 fps ones, so reconstruction sees every
    position on the way. Returns the raw readings with the dense stretches in."""
    done: list[tuple[float, float]] = []
    for _ in range(STALL_ROUNDS):
        new = [(max(0.0, t - STALL_PRE), t + STALL_POST) for t in _stalls(readings, dict(readings))
               if not any(a <= t <= b for a, b in done)]
        if not new:
            break
        for a, b in new:
            cached = frames.covers(a, b)
            dense = frames.read(a, b)
            readings = [r for r in readings if not a <= r[0] <= b] + dense
            readings.sort(key=lambda r: r[0])
            log(f"      re-read the stall at {a:.0f}-{b:.0f}s at {STALL_FPS} fps{' (cached)' if cached else ''}")
        done += new
    return readings
