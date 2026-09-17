"""Turn a stream of board readings into a list of games (SAN move lists).

Each sampled frame yields either a 64-char placement string (FEN reading order,
white-at-bottom assumption) or ``None`` when no board / low confidence.

A game begins whenever a reading matches the standard starting position in
either on-screen orientation, and ends at the next start position, a long gap
with no board, or the first board change we cannot explain with a short unique
sequence of legal moves.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import chess

START = "rnbqkbnr" + "p" * 8 + "." * 32 + "P" * 8 + "RNBQKBNR"
# same position seen with the black side at the bottom (board rotated 180 deg)
START_FLIPPED = START[::-1]

MAX_DEPTH = 6          # half-moves a single transition may bridge in one hop
MAX_MISSES = 10        # consecutive unexplainable readings before a game is closed
GAP_FRAMES = 10         # this many consecutive board-less frames ends a game
MAX_PLIES = 400
OPEN_DEPTH = 5         # a video that skips the opening frames: accept an early
OPEN_HAMMING = 16     # position implied uniquely from the standard start
START_TOLERANCE = 1     # allowed mismatched squares when spotting a start position
                        # (every real first move changes >= 2 squares, so 1 is safe)
START_OCC_TOLERANCE = 2  # ... when the piece silhouette is exactly the opening one
NODE_BUDGET = 100000   # per-transition search cap. Benchmarked against a real noisy
                        # video (40 sampled transitions): every *legitimate* bridge that
                        # ever resolved there did so in under 5,000 nodes - a bigger
                        # budget only spent more time confirming an unresolvable one
                        # really was unresolvable (up to 160x slower for identical
                        # output at 400,000). But a synthetic 5-ply bridge
                        # (test_multi_ply_gap_is_bridged) needed ~40,000, so the real
                        # floor isn't as low as that one video suggested - kept well
                        # above it for headroom. A cheap reachability test rejects
                        # impossible ones first, and _find_sequence's own move
                        # filtering prunes each node's
                        # branching to squares the transition actually touches)


@dataclass
class Game:
    start_seconds: float
    moves_san: list[str] = field(default_factory=list)
    # video timestamp each move was confirmed at (parallel to moves_san). When
    # a transition bridges several moves in one hop, every move in that hop
    # shares the timestamp of the reading that confirmed the resulting
    # position - the individual moves' real timestamps aren't observable, and
    # assigning them the confirming reading's timestamp is honest about that
    # rather than fabricating false precision by interpolating.
    move_seconds: list[float] = field(default_factory=list)
    # Remaining time (seconds) on each player's on-screen clock right after
    # each move, parallel to moves_san - populated by scan.py as a
    # post-reconstruction step (clock_read.py knows nothing about games or
    # moves, only about a video window's clock pixels), left empty when
    # clock reading wasn't attempted or didn't find a usable clock for this
    # game.
    clock_white_seconds: list[float | None] = field(default_factory=list)
    clock_black_seconds: list[float | None] = field(default_factory=list)
    clean: int = 0             # transitions resolved by a unique single legal move
    bridged: int = 0          # transitions resolved by a unique multi-move sequence
    skipped: int = 0          # readings stepped over as noise
    flipped: bool = False     # board was read with black at the bottom

    @property
    def ply(self) -> int:
        return len(self.moves_san)

    @property
    def confidence(self) -> float:
        steps = self.clean + self.bridged
        return self.clean / steps if steps else 0.0

    def pgn(self) -> str:
        out: list[str] = []
        for i, san in enumerate(self.moves_san):
            if i % 2 == 0:
                out.append(f"{i // 2 + 1}.")
            out.append(san)
        return " ".join(out)


def _placement_of(board: chess.Board) -> str:
    rows = board.board_fen().split("/")
    expanded = []
    for row in rows:
        for ch in row:
            expanded.append("." * int(ch) if ch.isdigit() else ch)
    return "".join(expanded)


def _hamming(a: str, b: str) -> int:
    return sum(1 for x, y in zip(a, b) if x != y)


def _occ(p: str) -> tuple:
    return tuple(ch != "." for ch in p)


START_OCC = _occ(START)   # 32 pieces home, ranks 3-6 empty (same when flipped)


def _is_start(placement: str) -> str | None:
    if _hamming(placement, START) <= START_TOLERANCE:
        return "normal"
    if _hamming(placement, START_FLIPPED) <= START_TOLERANCE:
        return "flipped"
    # a between-games board reset: exact opening silhouette, a couple misread squares
    if _occ(placement) == START_OCC:
        if _hamming(placement, START) <= START_OCC_TOLERANCE:
            return "normal"
        if _hamming(placement, START_FLIPPED) <= START_OCC_TOLERANCE:
            return "flipped"
    return None


FUZZ = 1                # squares of recognition noise tolerated when matching a
                        # target. A mid-drag frame (piece lifted, not yet landed)
                        # or similar can throw off two squares at once, but a
                        # tolerance loose enough to absorb that also accepts a
                        # wrong explanation (some *other* legal move landing
                        # within tolerance of a garbled reading) just as often as
                        # the right one - safer to under-resolve a noisy frame
                        # (skip it; see MAX_MISSES) than to confidently record
                        # the wrong move


def _reachable(cur: str, tgt: str) -> bool:
    """Cheap necessary condition for *tgt* being reachable from *cur* by legal
    moves - lets obviously-bogus readings skip the expensive search. One square
    of slack absorbs a lone misrecognised piece."""
    if _hamming(cur, tgt) <= 1:
        return False                     # no legal move changes just one square
    c, t = Counter(cur), Counter(tgt)
    for pawn, king, others in (("P", "K", "NBRQ"), ("p", "k", "nbrq")):
        if not 1 <= t[king] <= 1 + FUZZ:
            return False
        if t[pawn] - c[pawn] > FUZZ:     # pawns barely reappear
            return False
        promos = sum(max(0, t[o] - c[o]) for o in others)
        if promos > max(0, c[pawn] - t[pawn]) + FUZZ:
            return False                 # more promotions than pawns spent
        grew = sum(t[x] for x in pawn + king + others) - sum(c[x] for x in pawn + king + others)
        if grew > FUZZ:                  # a colour's material can't really grow
            return False
    return True


def _common_prefix(seqs: list[list[chess.Move]]) -> list[chess.Move]:
    out: list[chess.Move] = []
    for moves in zip(*seqs):
        if len(set(moves)) == 1:
            out.append(moves[0])
        else:
            break
    return out


def _square_of(index: int) -> chess.Square:
    """Map a placement-string index (FEN reading order: rank 8 first, file a
    first) to a python-chess square."""
    return chess.square(index % 8, 7 - index // 8)


def _relevant_squares(cur: str, target: str) -> set[chess.Square]:
    """Squares whose occupant differs between *cur* and *target*.

    Any move that touches none of these can't bring *cur* one step closer to
    *target* without being undone later, so it can never be part of a
    shortest bridging sequence - pruning it keeps the search's branching
    factor proportional to how much actually changed (a handful of squares)
    instead of the full ~30-40 legal moves at every node, which is what let a
    real gap of more than about three plies exhaust the node budget outright
    (see NODE_BUDGET) and silently stall reconstruction.
    """
    return {_square_of(i) for i in range(64) if cur[i] != target[i]}


def _find_sequence(board: chess.Board, target: str, max_depth: int):
    """Moves from *board* that we can be sure of on the way to placement *target*.

    An exact match (hamming 0) at any depth always wins over a merely-close one
    (within FUZZ) found at a shallower depth: nearly every chess move leaves
    exactly two squares "still wrong" relative to a target that's actually two
    or more moves further off, so a shallow fuzzy match is very often just an
    incomplete partial explanation, not recognition noise - a deeper exact
    match is almost certainly the true story and must be preferred. The
    shallowest fuzzy match is remembered as a fallback and only used if no
    exact match turns up by max_depth.

    Returns (moves, depth) with a unique/shared-prefix move list, or
    (None, reason) where reason is 'none', 'ambiguous' or 'budget'.
    """
    relevant = _relevant_squares(_placement_of(board), target)
    frontier = [(board.copy(stack=False), [])]
    budget = NODE_BUDGET
    fallback: tuple[int, list[chess.Move]] | None = None
    for depth in range(1, max_depth + 1):
        nxt = []
        exact = []                        # paths reaching hamming == 0
        fuzzy = []                         # (hamming, path), 0 < hamming <= FUZZ
        for b, path in frontier:
            for mv in b.legal_moves:
                if mv.from_square not in relevant and mv.to_square not in relevant:
                    continue               # can't help - prune (see _relevant_squares)
                budget -= 1
                if budget <= 0:
                    if fallback:
                        return fallback[1], fallback[0]
                    return None, "budget"
                b.push(mv)
                ham = _hamming(_placement_of(b), target)
                if ham == 0:
                    exact.append(path + [mv])
                else:
                    if ham <= FUZZ:
                        fuzzy.append((ham, path + [mv]))
                    # even a fuzzy match keeps going: it's only a fallback
                    # candidate, and a deeper exact match must still win if
                    # there is one (see docstring)
                    nxt.append((b.copy(stack=False), path + [mv]))
                b.pop()
        if exact:
            if len(exact) == 1:
                return exact[0], depth
            shared = _common_prefix(exact)
            if shared:
                return shared, depth      # only the moves every candidate agrees on
            return None, "ambiguous"
        if fuzzy and fallback is None:
            best = min(h for h, _ in fuzzy)
            cand = [p for h, p in fuzzy if h == best]
            shared = cand[0] if len(cand) == 1 else _common_prefix(cand)
            if shared:
                fallback = (depth, shared)
        frontier = nxt
        if not frontier:
            break
    if fallback:
        return fallback[1], fallback[0]
    return None, "none"


def _distinct_states(readings: list[tuple[float, str | None]]):
    """Collapse repeats; yield ('state', ts, placement) and ('gap', ts, None)."""
    out = []
    last = None
    missing = 0
    for ts, placement in readings:
        if placement is None:
            missing += 1
            if missing == GAP_FRAMES:
                out.append(("gap", ts, None))
                last = None
            continue
        missing = 0
        if placement != last:
            out.append(("state", ts, placement))
            last = placement
    return out


def reconstruct(readings: list[tuple[float, str | None]]) -> list[Game]:
    events = _distinct_states(readings)
    games: list[Game] = []
    current: Game | None = None
    board: chess.Board | None = None

    def close():
        nonlocal current, board
        if current and current.ply >= 2:
            games.append(current)
        current, board = None, None

    misses = 0
    for kind, ts, placement in events:
        if kind == "gap":
            close()
            continue

        orient = _is_start(placement)
        if orient is not None:
            close()
            current = Game(start_seconds=ts, flipped=(orient == "flipped"))
            board = chess.Board()
            misses = 0
            continue

        if current is None or board is None:
            # the video may have skipped the first moves - open a game if this
            # reading is a unique short continuation of the standard start
            if _hamming(placement, START) <= OPEN_HAMMING or \
               _hamming(placement, START_FLIPPED) <= OPEN_HAMMING:
                for flip in (False, True):
                    tgt = placement[::-1] if flip else placement
                    seq, _info = _find_sequence(chess.Board(), tgt, OPEN_DEPTH)
                    if seq:
                        current = Game(start_seconds=ts, flipped=flip)
                        board = chess.Board()
                        for mv in seq:
                            current.moves_san.append(board.san(mv))
                            current.move_seconds.append(ts)
                            board.push(mv)
                        current.clean += 1
                        misses = 0
                        break
            continue

        target = placement[::-1] if current.flipped else placement
        cur_placement = _placement_of(board)
        if target == cur_placement:
            misses = 0
            continue

        # Always search to the full depth on the first (and possibly only) try:
        # _find_sequence already returns as soon as it finds a solution at the
        # shallowest depth, so this costs nothing for the common single-move
        # case. Scaling depth up with `misses` instead would only pay off on a
        # *retry*, but a target that keeps reading the same way (the game
        # paused, or the video just resumed on a stable position) never
        # produces a fresh "state" event to retry on - _distinct_states
        # collapses repeats - so it would only ever get the one, shallow, attempt.
        if _reachable(cur_placement, target):
            moves, info = _find_sequence(board, target, MAX_DEPTH)
        else:
            moves, info = None, "unreachable"
        if moves is None:
            # a noisy / mid-animation reading - step over it and try to resync
            misses += 1
            current.skipped += 1
            if misses >= MAX_MISSES:
                close()
            continue

        misses = 0
        for mv in moves:
            current.moves_san.append(board.san(mv))
            current.move_seconds.append(ts)
            board.push(mv)
        if len(moves) == 1:
            current.clean += 1
        else:
            current.bridged += 1

        if board.is_checkmate() or board.is_stalemate():
            # no legal move exists from here - anything read after this point
            # cannot be a continuation of this game. Without this, a player
            # reviewing the finished game (playing out "what if" lines in
            # analysis mode, which looks like an ordinary legal continuation
            # from the board alone) would get silently grafted onto the real
            # game's move list as if it had actually been played.
            close()
            continue

        if current.ply >= MAX_PLIES:
            close()

    close()
    return games
