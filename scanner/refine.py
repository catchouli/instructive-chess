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

from pathlib import Path

import chess

import recognize
import reconstruct

DENSE_FPS = 6          # frames per second inside a bunch's window
MIN_BUNCH = 2          # plies confirmed together that are worth re-reading
STABLE_FRAMES = 2      # a dense placement must repeat this often to count
WINDOW_PAD = 3.0       # seconds either side of a bunch to re-read: its 1 fps time comes
                       # from a +-2 frame vote, so the moves can sit a couple of seconds off


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


def refine_game(path: Path, box, model, game: reconstruct.Game, iter_frames) -> int:
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

        dense: list[tuple[float, str]] = []
        for ts, frame in iter_frames(path, DENSE_FPS, max(0.0, t_begin - WINDOW_PAD), t_end + WINDOW_PAD):
            rd = recognize.read_board(frame, box, model)
            if rd.confidence >= 0.72 and 4 <= rd.n_pieces <= 34:
                dense.append((ts, rd.placement[::-1] if game.flipped else rd.placement))
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
