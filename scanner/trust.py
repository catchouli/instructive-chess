"""How far into a reconstructed game its moves can be trusted.

The catalog is for finding games by their moves, so a wrong move is worse
than a shorter game. Every move is kept in the data, but each gets a
confidence and each game a `trusted_ply`: the web app only shows (and
searches) moves up to it.

The rule is calibrated against real games (tests/benchmark.py), using only
what reconstruction leaves in the catalog - the moves and the video time
each was confirmed at. Moves confirmed together in one reading ("bunched")
were inferred rather than each seen, and that's where games go wrong:

  bunch of 2, first 30 plies   122 right,  1 wrong
  bunch of 2, later            169 right,  9 wrong
  bunch of 3 or more            33 right, 12 wrong (4+: 4 right, 9 wrong)
  (first wrong moves seen on their own: 19, among ~4,800 such plies)

and a misread move is often what causes the jumble that follows, so trust
ends a ply or two *before* such a bunch. A game's last couple of moves are risky
too (a result screen or post-game analysis creeping in), unless the game
ends in mate. Recalibrated 2026-09-25 on 136 benchmarked games (more found
after recognition fixes): late-bunch threshold 30 -> 26 and a 2-ply margin
before a trigger bunch, for 0 games with a wrong trusted move (the old
settings left 3: two wrong early pairs, one misread two plies before a
bunch), trusting 68% of plies. Recalibrated again 2026-10-14 on 209 games
(the library rescanned with the blitz-stream fixes plus eight Titled
Tuesday streams): bunches accepted from a reading a square off (`fuzzy`)
are wrong 5% of the time even when the next move is seen exactly, so they
end trust; exact pairs early are wrong 0.3% of the time (288 right, 1
wrong) and pairs between plies 26 and 34 added no error, so the late
threshold went back up to 34: 0 wrong of 5,329 trusted plies, the whole
15-move opening trusted in 87 games instead of 71. Tuned on the games
it's measured on - expect a small error rate on new ones, and recheck as
the library grows.
"""
from __future__ import annotations

import chess

BIG_BUNCH = 3          # plies confirmed in one reading that end trust
LATE_PLY = 34          # past here, any bunch (2+) ends trust
TRIGGER_MARGIN = 2     # trust ends this many plies before such a bunch
TAIL_PLIES = 2         # always hold back this many final plies (unless mate)
LATE_TAIL_PLIES = 4    # ...and this many, past LATE_PLY
ROUGH_BACKOFF = 5      # trust ending this soon after a rough ply ends at it instead

# Chance a move is right, by how it was confirmed (the calibration above).
CONF_SINGLE = 0.996
CONF_BUNCH_2_EARLY = 0.99
CONF_BUNCH_2_LATE = 0.95
CONF_BUNCH_BIG = 0.7
CONF_TAIL = 0.8        # a rough figure: tail errors were ~a third of all first errors


def _bunches(move_seconds: list[float]) -> list[tuple[int, int]]:
    """(start, size) of the run of equal timestamps each ply belongs to."""
    out = []
    n = len(move_seconds)
    for i in range(n):
        j = i
        while j > 0 and move_seconds[j - 1] == move_seconds[i]:
            j -= 1
        k = i
        while k + 1 < n and move_seconds[k + 1] == move_seconds[i]:
            k += 1
        out.append((j, k - j + 1))
    return out


def _ends_the_game(moves_san: list[str]) -> bool:
    board = chess.Board()
    for san in moves_san:
        board.push_san(san)
    return board.is_checkmate() or board.is_stalemate()


CONF_GUESSED = 0.5     # a route guessed among transpositions (reconstruct.Game.ambiguous)
CONF_FUZZY = 0.9       # a bunch accepted from a reading a square off (reconstruct.Game.fuzzy)


def assess(moves_san: list[str], move_seconds: list[float],
           clocks_checked_end: bool = False, ambiguous: list[int] = (),
           fuzzy: list[int] = (), doubtful: list[int] = (),
           rough: list[int] = ()) -> tuple[list[float], int]:
    """(per-move confidence, trusted_ply) for one game. `clocks_checked_end`:
    both clocks were read, so clock_read.live_move_count has already checked
    that a final mate happened with a clock running - only then does a mate
    waive the end-of-game holdback (videos also show mating lines after a
    resignation). `ambiguous`: plies whose moves were a guess among routes
    to the same position; trust ends before the first, like before a bunch.
    `fuzzy`: plies of a bunch accepted from a reading a square off - the
    same treatment (a wrong such pair leaked 16 trusted plies). `doubtful`:
    plies accepted under doubt (after an undo, browsing back or a detour, or
    a bunch after an unexplainable reading; see reconstruct.Game.doubtful) -
    the same again. `rough`: first plies of bunches accepted right after an
    unexplainable reading a square off (reconstruct.Game.rough). Too common
    to end trust on their own (measured: 55% of whole openings lost), but
    every wrong trusted move left in 1,620 benchmarked games came 1-5 plies
    before where trust already ended, just after one or more of these - so
    trust ending within ROUGH_BACKOFF plies after one ends at it instead
    (9 such games -> 0, for 4.5% fewer games shown)."""
    n = len(moves_san)
    bunches = _bunches(move_seconds)
    conf = []
    trusted = n
    for i, (start, size) in enumerate(bunches):
        if size >= BIG_BUNCH:
            conf.append(CONF_BUNCH_BIG)
        elif size == 2:
            conf.append(CONF_BUNCH_2_EARLY if start < LATE_PLY else CONF_BUNCH_2_LATE)
        else:
            conf.append(CONF_SINGLE)
        if trusted == n and i == start and (size >= BIG_BUNCH or (size >= 2 and start >= LATE_PLY)):
            trusted = max(0, start - TRIGGER_MARGIN)
    for i in ambiguous:
        if i < n:
            conf[i] = min(conf[i], CONF_GUESSED)
    for i in fuzzy:
        if i < n:
            conf[i] = min(conf[i], CONF_FUZZY)
    for i in doubtful:
        if i < n:
            conf[i] = min(conf[i], CONF_FUZZY)
    for marks in (ambiguous, fuzzy, doubtful):
        if marks:
            trusted = min(trusted, max(0, min(marks) - TRIGGER_MARGIN))
    if not (clocks_checked_end and _ends_the_game(moves_san)):
        tail_start = min(n - TAIL_PLIES, max(min(LATE_PLY, n - TAIL_PLIES), n - LATE_TAIL_PLIES))
        for i in range(max(0, tail_start), n):
            conf[i] = min(conf[i], CONF_TAIL)
        trusted = min(trusted, tail_start)
    near = [i for i in rough if trusted - ROUGH_BACKOFF <= i < trusted]
    if near:
        trusted = min(near)
    return [round(c, 3) for c in conf], max(0, trusted)
