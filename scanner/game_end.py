"""Where a game ends, from its clocks: moves after which neither clock runs
again are post-game (analysis on the board, a result popup covering it) and
are dropped.

Kept apart from clock_read.py on purpose: the clock cache
(scanner/cache/<id>-clocks.json) is keyed by a fingerprint of clock_read's
source, so a change to these rules - which only use the readings - doesn't
make a rescan read every clock again.
"""
from __future__ import annotations

import os

from clock_read import ClockReading

# A ply played while a clock was running in the LIVE_BEFORE_S seconds up to
# it was played in a live game, whatever follows it - a real last move after
# which the opponent resigned at once leaves no clock running (0 = off).
LIVE_BEFORE_S = float(os.environ.get("GAME_END_LIVE_BEFORE", "0"))
# A mate ends the game when a clock ran in the MATE_BEFORE_S seconds up to
# it (0 = only since the ply before, which is empty when both were read in
# one frame).
MATE_BEFORE_S = float(os.environ.get("GAME_END_MATE_BEFORE", "0"))


def _ran(a: float, b: float) -> bool:
    """A clock read a then b, a frame later, went down: by a whole second, or
    by more than half of one when it shows tenths (under 20 s). Read once a
    second, a clock showing tenths falls by 0.8-1.2 between frames, and
    "a whole second" missed 18.1 -> 17.3: the game's last move was dropped
    while the game went on (-rqPeGKVPbA 2658, 47 plies still to come)."""
    return b <= a - 1 or (a != int(a) and b < a - 0.5)


def live_move_count(move_times: list[float], readings_by_side: dict[str, list[ClockReading]],
                    game_over_after=lambda n: False) -> int:
    """How many of a game's moves were played while the game was still on:
    trailing moves after which neither clock ever runs again are post-game
    and are dropped.

    A real move is always followed by the opponent's clock running - unless
    it ended the game on the board, which `game_over_after(n)` (checkmate or
    stalemate after the first n moves) reports. Confirmed on real videos:
    4 moves "played" in 7s after the game ended with both clocks frozen, and
    a line shown on the board after the opponent resigned.

    Needs both clocks, and clocks seen running during the game - otherwise a
    missing or frozen reading could look like "nobody's clock moved" and
    this keeps every move. Never trims more than half the game, which would
    point at a clock-reading problem rather than a post-game tail.
    """
    n = len(move_times)
    if n == 0 or len(readings_by_side) < 2:
        return n

    def runs_after(t: float) -> bool:
        # A running clock goes *down*: a rise after the game is over (a new
        # game's clock appearing, a results screen - confirmed on a real
        # video, 433 -> 439 after the last move) must not count. From the
        # first 1 fps clock frame after the move: a move timed between
        # whole seconds at 6 fps (130.33) with ">= t + 1" skipped the frame
        # at 131 and lost the one tick before a quick resignation, dropping
        # the game's last move (41 games on 315 videos).
        for readings in readings_by_side.values():
            vals = [r.seconds for r in readings if r.seconds is not None and r.t > t + 0.5]
            if any(_ran(a, b) for a, b in zip(vals, vals[1:])):
                return True
        return False

    def ran_between(t0: float, t1: float) -> bool:
        for readings in readings_by_side.values():
            vals = [r.seconds for r in readings if r.seconds is not None and t0 < r.t <= t1]
            if any(_ran(a, b) for a, b in zip(vals, vals[1:])):
                return True
        return False

    def ran_before(k: int) -> bool:
        # did any clock go down in the run-up to ply k - someone thinking?
        t = move_times[k - 1]
        t0 = move_times[k - 2] if k >= 2 else move_times[0] - 30.0
        if MATE_BEFORE_S:
            t0 = min(t0, t - MATE_BEFORE_S)
        return ran_between(t0, t + 0.5)

    def live(k: int) -> bool:
        t = move_times[k - 1]
        return bool(LIVE_BEFORE_S) and ran_between(t - LIVE_BEFORE_S, t + 0.5)

    if not runs_after(move_times[0] - 1.0):
        return n
    keep = n
    # A mate only ends the game if someone's clock was running as it was
    # found: instructive videos show mating lines after a resignation too,
    # with both clocks frozen (confirmed on a real video: six analysis
    # plies ending "Rxf8#" were kept as the game's real ending).
    while keep > 0 and not (game_over_after(keep) and ran_before(keep)) \
            and not live(keep) and not runs_after(move_times[keep - 1]):
        keep -= 1
    return keep if keep >= n / 2 else n
