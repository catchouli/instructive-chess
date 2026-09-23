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
DETOUR_MAX_PLIES = 8   # longest shown-then-undone line retracted (see the detour check)
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
    # Player ratings, e.g. the "(2653)" next to a name - populated by scan.py
    # from a single high-resolution frame (see rating_read.py; the regular
    # scan resolution is too low to read rating digits from). A rating never
    # changes mid-game, so unlike the clock fields this is one value per
    # side, not one per move. None when rating reading wasn't attempted or
    # didn't find a confident reading for that side.
    rating_white: int | None = None
    rating_black: int | None = None
    # The two clock widgets found for this game, (x, y, w, h) at scan
    # resolution, upper first - rating reading uses them as an anchor (see
    # rating_read.read_ratings_beside_clocks). Empty unless both were found.
    clock_boxes: list[tuple[int, int, int, int]] = field(default_factory=list)
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


def _has_wasted_round_trip(board: chess.Board, path: list[chess.Move]) -> bool:
    """True if some *specific piece* moves away from its starting square at
    some point in *path* and later ends up back on that exact same square.

    Such a piece accomplishes nothing on the board - real games don't do
    this except in vanishingly rare repetition-seeking lines, so a candidate
    sequence containing one is almost always a coincidental, spurious way of
    matching a *wrong* target (typically a single bad frame misreading a
    piece as simply missing) rather than the true line of play. Confirmed on
    a real video: a single frame that misread one pawn capture as an empty
    square got "explained" by a legal-but-nonsensical 5-ply sequence - a
    bishop and a rook both taking an excursion and returning to their exact
    starting squares - purely because it happened to reach an exact pixel
    match, badly corrupting every move recorded after it.

    This tracks individual piece identity (which square each surviving piece
    *originated* from before `path`) rather than comparing each square's
    occupant type/color before and after: an ordinary capture-then-recapture
    on one square (e.g. exd4 cxd4, trading off an isolated pawn) leaves that
    square occupied by the *same kind* of piece it started with, but it's a
    genuinely different physical pawn - the original occupant was captured
    and is gone, not "returned". Comparing only the resulting type/color (an
    earlier version of this check) couldn't tell those apart and flagged
    ordinary recaptures as wasted round trips."""
    # origin[sq] is the square this piece occupied before `path` began, for
    # every piece still on the board; a piece present at the start with no
    # entry hasn't moved (yet), so can't have "returned" - it never left.
    origin: dict[chess.Square, chess.Square] = {}
    b = board.copy(stack=False)
    for mv in path:
        captured_sq = None
        if b.is_en_passant(mv):
            captured_sq = mv.to_square + (-8 if b.turn == chess.WHITE else 8)
        elif b.piece_at(mv.to_square) is not None:
            captured_sq = mv.to_square
        if captured_sq is not None:
            origin.pop(captured_sq, None)

        moved_from = origin.pop(mv.from_square, mv.from_square)
        origin[mv.to_square] = moved_from

        if b.is_castling(mv):
            rank = chess.square_rank(mv.from_square)
            if b.is_kingside_castling(mv):
                rook_from, rook_to = chess.square(7, rank), chess.square(5, rank)
            else:
                rook_from, rook_to = chess.square(0, rank), chess.square(3, rank)
            rook_origin = origin.pop(rook_from, rook_from)
            origin[rook_to] = rook_origin

        b.push(mv)

    # `origin` only has an entry for a square once some piece has actually
    # moved through it during `path` (see the pop(..., default) above), so
    # membership alone means "this piece moved at least once" - no separate
    # "did it ever leave" bookkeeping needed.
    return any(cur == orig for cur, orig in origin.items())


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

    An exact match is only trusted if it doesn't contain a wasted round trip
    (see _has_wasted_round_trip) - otherwise it's discarded and the search
    keeps going, since a "deeper exact match" that only achieves the target
    by shuffling a piece there and back is exactly the kind of spurious
    coincidence that preferring exact matches was never meant to reward.

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
            exact = [p for p in exact if not _has_wasted_round_trip(board, p)]
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


def _clock_at_or_before(pairs: list[tuple[float, float]], t: float) -> float | None:
    """The clock value from the last entry in `pairs` (sorted by timestamp)
    at or before `t`, or None if `pairs` has no entry that early."""
    best = None
    for pt, seconds in pairs:
        if pt <= t:
            best = seconds
        else:
            break
    return best


def _clock_contradicts_bridge(clock_by_side: dict[str, list[tuple[float, float]]] | None,
                               board: chess.Board, moves: list[chess.Move],
                               t0: float, t1: float, min_gap: float = 2.0) -> bool:
    """True if the clock proves at least one side credited with a move in
    this bridge never actually spent any time on it.

    A real move always costs the mover some of their own remaining time (or
    at worst leaves it unchanged via an increment bonus, never literally the
    same reading with no tick at all across several real seconds) - a
    reading that can only be explained by crediting a side with a move its
    own clock flatly contradicts is a strong sign the reading itself isn't
    real play. Confirmed on a real video: a cancelled/changed premove
    (dragged to one square, then actually played to a different one before
    the opponent moved) put a piece on the board for several sampled frames
    that never became a real move - board content alone can't tell that
    apart from a genuine move, but the mover's clock not moving at all
    across multiple real seconds can.

    `min_gap` guards against the ordinary case of a fast move landing on the
    very next 1-second sample, where the clock's whole-second display just
    hasn't visibly ticked yet - only treat a *sustained* lack of ticking
    (several real seconds with literally no clock evidence of thought) as
    contradicting the move.
    """
    if not clock_by_side or t1 - t0 < min_gap:
        return False
    sides_moved: set[str] = set()
    b = board.copy(stack=False)
    for mv in moves:
        sides_moved.add("white" if b.turn == chess.WHITE else "black")
        b.push(mv)
    for side in sides_moved:
        pairs = clock_by_side.get(side)
        if not pairs:
            continue
        before = _clock_at_or_before(pairs, t0)
        after = _clock_at_or_before(pairs, t1)
        if before is not None and after is not None and after >= before:
            return True
    return False


def reconstruct(readings: list[tuple[float, str | None]],
                 clock_by_side: dict[str, list[tuple[float, float]]] | None = None) -> list[Game]:
    events = _distinct_states(readings)
    games: list[Game] = []
    current: Game | None = None
    board: chess.Board | None = None
    last_ts = 0.0

    # Placements after each ply of the current game (history[i] = after i
    # plies), to recognise the viewer browsing back through the move list
    # mid-game - see the browsing check in the loop below.
    history: list[str] = []
    browsing = False

    def close():
        nonlocal current, board, history, browsing
        if current and current.ply >= 2:
            games.append(current)
        current, board = None, None
        history, browsing = [], False

    def retract_to(k: int) -> None:
        nonlocal board
        del current.moves_san[k:], current.move_seconds[k:], history[k + 1:]
        board = chess.Board()
        for san in current.moves_san:
            board.push_san(san)

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
            history = [_placement_of(board)]
            misses = 0
            last_ts = ts
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
                        history = [_placement_of(board)]
                        for mv in seq:
                            current.moves_san.append(board.san(mv))
                            current.move_seconds.append(ts)
                            board.push(mv)
                            history.append(_placement_of(board))
                        current.clean += 1
                        misses = 0
                        last_ts = ts
                        break
            continue

        target = placement[::-1] if current.flipped else placement
        cur_placement = _placement_of(board)
        if target == cur_placement:
            misses = 0
            browsing = False
            continue

        # The viewer stepping back through the move list mid-game (confirmed
        # on several real videos) shows earlier positions, and stepping back
        # is itself a legal-looking move ("Be6" undone reads as "Bc8"), so
        # the game went wrong from there. Real play can't produce this: the
        # position before the last ply can't recur after one more ply (the
        # opponent can't move your piece back), and a position an even
        # number of plies back needs both players to undo their own moves
        # within a single reading. An odd number of plies back (3+) *can*
        # be a real repetition (Nf3 Nf6 Ng1 Ng8...), so that's left to
        # ordinary processing - unless already browsing.
        n = len(history) - 1
        earlier = [k for k in range(n) if history[k] == target]
        if earlier and (browsing or earlier[-1] == n - 1 or (n - earlier[-1]) % 2 == 0):
            browsing = True
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
        if moves is not None and _clock_contradicts_bridge(clock_by_side, board, moves, last_ts, ts):
            moves, info = None, "clock-contradicted"
        if moves is None:
            # a noisy / mid-animation reading - step over it and try to resync
            misses += 1
            current.skipped += 1
            if misses >= MAX_MISSES:
                close()
            continue

        misses = 0
        last_ts = ts
        browsing = False
        for mv in moves:
            current.moves_san.append(board.san(mv))
            current.move_seconds.append(ts)
            board.push(mv)
            history.append(_placement_of(board))
        if len(moves) == 1:
            current.clean += 1
        else:
            current.bridged += 1

        # A detour: the board went ahead (or sideways) and came back to a
        # position already reached - confirmed on real recap videos, a
        # future line was shown and then stepped back ("Bf5 Nd4" then
        # "Bc8 N4f3"), each step a legal-looking move, so the game got 4
        # extra plies. A real repetition (Nf3 Nf6 Ng1 Ng8) has the same
        # shape but its moves come one per reading with time between;
        # a detour's come bunched, several plies seen at once. So a return
        # within DETOUR_MAX_PLIES that includes bunched plies is retracted.
        n = len(history) - 1
        back = next((k for k in range(n - 1, max(-1, n - DETOUR_MAX_PLIES - 1), -1)
                     if history[k] == history[n]), None)
        if back is not None:
            loop_times = current.move_seconds[back:n]
            if len(set(loop_times)) < len(loop_times):
                retract_to(back)

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
