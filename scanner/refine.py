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

DENSE_FPS = 6          # frames per second inside a bunch's window (15 was tried as a
                       # second pass for unsplit bunches: no gain - what's left is mostly
                       # premoves, whose in-between position is never drawn at all)
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


def split_bunch(before: str, path_placements: list[str], dense: list[tuple[float, str]],
                t_end: float) -> list[float] | None:
    """When each position along a bunch's known path was first seen in the
    dense frames - its moves stay exactly as reconstructed; only their
    times are refined.

    Positions are compared only on the squares the bunch's moves change
    (`before` is the position before it), and must match there *exactly*
    for STABLE_FRAMES consecutive dense frames, in path order. A dense
    frame has no flicker vote, so a misread square elsewhere on the board
    is common - and irrelevant to these moves (confirmed on a real video:
    one persistent misread off the moves' squares hid every split); a
    wrong piece on a square the moves *do* touch (a real misread: Ra7 for
    Qa7) is exactly what must not confirm a move. None unless every ply
    was seen on its own (see the all-or-nothing note below)."""
    positions = [before] + path_placements
    squares = [i for i in range(len(before)) if len({p[i] for p in positions}) > 1]
    keys = [tuple(q[i] for i in squares) for q in path_placements]
    k_seen: dict[int, float] = {}
    run_k, run_start, run_n = None, None, 0
    last_k = -1
    for t, p in dense + [(None, None)]:
        k = None
        if p is not None:
            key = tuple(p[i] for i in squares)
            if key in keys:
                k = keys.index(key)
        if k == run_k and k is not None:
            run_n += 1
            continue
        if run_k is not None and run_n >= STABLE_FRAMES and run_k > last_k:
            k_seen[run_k] = run_start
            last_k = run_k
        run_k, run_start, run_n = k, t, 1
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
        before = reconstruct._placement_of(board)
        path_placements = []
        for san in game.moves_san[start:start + size]:
            board.push_san(san)
            path_placements.append(reconstruct._placement_of(board))

        dense: list[tuple[float, str]] = []
        for ts, frame in iter_frames(path, DENSE_FPS, max(0.0, t_begin - WINDOW_PAD), t_end + WINDOW_PAD):
            rd = recognize.read_board(frame, box, model)
            if rd.confidence >= 0.72 and 4 <= rd.n_pieces <= 34:
                dense.append((ts, rd.placement[::-1] if game.flipped else rd.placement))
        times = split_bunch(before, path_placements, dense, t_end)
        if times is None:
            continue
        # strictly after the previous ply (or the split would merge into its
        # bunch), never after the bunch's own 1 fps confirmation
        game.move_seconds[start:start + size] = [round(min(max(t, t_begin + 0.1), t_end), 2) for t in times]
        improved += len(set(times))
    return improved
