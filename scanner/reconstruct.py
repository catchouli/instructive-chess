"""Turn a stream of board readings into a list of games (SAN move lists).

Each sampled frame yields either a 64-char placement string (FEN reading order,
white-at-bottom assumption) or ``None`` when no board / low confidence.

A game begins whenever a reading matches the standard starting position in
either on-screen orientation, and ends at the next start position, a long gap
with no board, or the first board change we cannot explain with a short unique
sequence of legal moves.
"""
from __future__ import annotations

import bisect
import os
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
LOOKAHEAD_S = 12.0     # a multi-ply bridge is checked against the next reading
                       # this close (see the lookahead in reconstruct)
LOOKAHEAD = True
LOOKAHEAD_FUZZ = True  # the next reading may be FUZZ squares off its shorter explanation
GUESS_MAX_ROUTES = 3   # an ambiguous reading is still followed (route guessed) when it has
GUESS_MAX_PLIES = 3    # at most this many routes of at most this many plies (a knight's
                       # route to one square); bigger tangles produced rook shuffles
UNDO = True            # the undo check in reconstruct
UNDO_WINDOW_S = 6.0    # a ply reverted within this long of appearing is taken back outright
BUNCH_UNDO = True      # ...and so is a whole bunch accepted from one reading
FUZZY_CONFIRM_S = 2.0  # a fuzzy bunch's position read exactly this long confirms it (0 = never)
UNREACH_BUNCH = 0.0  # a bunch accepted right after an unexplainable reading a square off ends trust (0 = off)
RETRACT_ENDS_TRUST = 1.0  # moves accepted right after an undo or detour end trust (0 = off)
REPLAY_SHARED = 10     # a game that opens with this many of an earlier game's plies...
REPLAY_FAST = 14       # ...and has this many plies within REPLAY_WINDOW_S anywhere
REPLAY_WINDOW_S = 4.0  # is the narrator stepping back through that game: dropped (0 = off)
REPLAY_FROZEN = True   # ...as is one opening with REPLAY_SHARED of them whose clocks never ran
RETRACT_BROWSE = False    # ...and after browsing back (3 -> 1 leaks either way for 2.5x the cost)
BROWSE_STEP_S = 3.0       # a ply this recent that moved the board toward the earlier position the viewer
                          # then browsed to is taken back: a frame caught mid-step (0 = off)
BROWSE_STEP_PLIES = 2     # ...up to this many (on 14 videos, 2 gave 8,438 right plies and 1 gave 8,402)
BRIEF_UNDO_NEAR = 3       # ...but one ply shown briefly and taken back only within this many plies of an
                          # unexplainable reading a square off (0 = always): see EXPERIMENTS.md
BRIEF_UNDO_RAW = int(os.environ.get("RECON_BRIEF_UNDO_RAW", "1"))
                          # ...or when the ply after it comes from a raw frame (0 = off; traced: BdcY0WCN29Y
                          # 7.Qd2 read, taken back as brief, then "7.c3" from a raw frame trusted). Whole
                          # library: it changes 2 games - that one (wrong trusted -> trust ends at ply 10)
                          # and qv6_noaf7BA-8 (25 -> 20 trusted, all right)
RAW_FALLBACK = 1.0  # a voted reading nothing explains is tried again as the raw frame (0 = off): see EXPERIMENTS.md
RAW_DOUBTFUL = 3.0  # ...and its moves end trust: 1 = when the voted reading was a square off, 2 = always,
                    # 3 = when they and the next reading's make a big bunch, 0 = never
RAW_SPLIT_S = 5.0   # ...the next reading this soon after
RAW_SPLIT_BUNCH = 3  # ...together this many plies (trust.BIG_BUNCH)
RAW_CONFIRM = 2.0   # a raw frame's moves end trust when the next distinct reading doesn't follow from them:
                    # 1 = always, 2 = only when the moved piece going elsewhere explains it better (0 = off)
HIDDEN_KING = 1.0  # a missing king: try castling, and moves by its side are guesses (0 = off)
RECOVER = 1.0          # readings stopped following: re-explain them from a few plies back (0 = off,
                       # 1 = not for a reading with several routes from here, 2 = for those too)
RECOVER_PLIES = 6      # ...from at most this many plies back
RECOVER_MIN_DIFF = 3   # ...for a reading at least this many squares from the current position
RECOVER_QUIET_S = 30.0  # ...and not this soon after the board went back (a line being shown, not played)
RECOVER_STRICT = 0.0   # ...and never to a shorter line, nor from a reading with a king missing (0 = off)
PREMOVE_MAX_DIFF = 6   # a premove drawn on a move changes at most this many squares
LOOKAHEAD_MAX_DIFF = 8      # _explains_next: the next reading must be within a move or two
LOOKAHEAD_DEPTH = 4         # ...searched this deep at most
LOOKAHEAD_MAX_VARIANTS = 4  # ...trying at most this many premove-stripped variants of it
OPEN_DEPTH = 5         # a video that skips the opening frames: accept an early
OPEN_HAMMING = 16     # position implied uniquely from the standard start
REOPEN_OTHER_SIDE = int(os.environ.get("RECON_REOPEN_OTHER_SIDE", "1"))
REOPEN_DEPTH = 10     # ...searched this deep: by the time the old game is that far off, the new one is
                      # 7-8 plies in
REOPEN_FAR = 16       # (0 = off) While a game is open, a reading at least this many squares
                      # from it that is such a position with the board the other way up is a new game
                      # (traced: 8C69k-ce4-c, the waiting screen showed the start from Black's side, the
                      # site turned the board for a game as White, and the open "game" read the new one
                      # upside down until it closed, too far in to open mid-game). Whole library:
                      # matched +5 (7 new, 2 lost), right plies +140, trusted +59, 0 wrong; 3 games end
                      # 1-2 plies earlier, trust drops in 2
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
    # plies whose exact moves are a guess among routes to the same position
    # (3.Nd2 dxe4 4.Nxe4 or 3.Nc3 dxe4 4.Nxe4, both seen only as the
    # result) - the position is certain, the move order isn't; trust.py
    # never trusts these
    ambiguous: list[int] = field(default_factory=list)
    # plies of a bunch accepted from a reading a square off (FUZZ): the
    # position wasn't seen exactly, so the moves are a best fit - trust.py
    # stops before them (a wrong such pair leaked 16 trusted plies once)
    fuzzy: list[int] = field(default_factory=list)
    # plies accepted under doubt - right after an undo, browsing back or a
    # detour retract (measured on 1,650 games: the first move after such an
    # event was wrong 3-35% of the time, vs 0.3% after nothing unusual), or
    # a bunch accepted right after an unexplainable reading a square off
    # (UNREACH_BUNCH) - trust.py stops before them
    doubtful: list[int] = field(default_factory=list)
    # first plies of bunches accepted right after an unexplainable reading a
    # square off: where trust ends just after one, trust.py backs off to it
    rough: list[int] = field(default_factory=list)
    # per ply: was the position after it read exactly again before the next
    # ply (or within SETTLE_TAIL_S of the last)? A game's final plies that
    # never were are where reconstruction derailed (see catalog.drop_unsettled_tail)
    settled: list[bool] = field(default_factory=list)
    # per ply, for the logs: the reading closest to it in that window - how
    # many squares off, how many frames came that close, which squares
    settle_near: list[str] = field(default_factory=list)

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
    # straight from the bitboards: board_fen() was most of reconstruction's time
    out = ["."] * 64
    white = board.occupied_co[chess.WHITE]
    for bb, sym in ((board.pawns, "p"), (board.knights, "n"), (board.bishops, "b"),
                    (board.rooks, "r"), (board.queens, "q"), (board.kings, "k")):
        for sq in chess.scan_forward(bb):
            out[sq ^ 56] = sym.upper() if white >> sq & 1 else sym
    return "".join(out)


_CASTLE_ROOK = {chess.G1: (chess.H1, chess.F1), chess.C1: (chess.A1, chess.D1),
                chess.G8: (chess.H8, chess.F8), chess.C8: (chess.A8, chess.D8)}


def _changed_squares(board: chess.Board, mv: chess.Move) -> tuple:
    """The squares a move changes (before it's pushed): from and to, a
    castling rook's two, an en passant capture's pawn."""
    if board.is_castling(mv):
        return (mv.from_square, mv.to_square) + _CASTLE_ROOK[mv.to_square]
    if board.is_en_passant(mv):
        return mv.from_square, mv.to_square, mv.to_square + (-8 if board.turn == chess.WHITE else 8)
    return mv.from_square, mv.to_square


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


_SEARCHES: dict = {}          # (position, target, depth, budget, alternatives?) -> result, alternatives found
_SEARCHES_MAX = 200_000


def _find_sequence(board: chess.Board, target: str, max_depth: int, budget: int = NODE_BUDGET,
                   alternatives: list | None = None):
    """_search, remembered: the stall re-read (refine.reread_stalls) runs
    reconstruction twice more over mostly the same readings, which asked the
    same questions again. A search depends only on the position (not how it
    was reached), so the remembered answer is the one it would give."""
    if alternatives:                      # filled already: not a question asked before
        return _search(board, target, max_depth, budget, alternatives)
    key = (" ".join(board.fen().split()[:4]), target, max_depth, budget, alternatives is not None)
    hit = _SEARCHES.get(key)
    if hit is None:
        found: list | None = [] if alternatives is not None else None
        result = _search(board, target, max_depth, budget, found)
        if len(_SEARCHES) >= _SEARCHES_MAX:
            _SEARCHES.clear()
        hit = _SEARCHES[key] = (result, found)
    (moves, info), found = hit
    if alternatives is not None:
        alternatives.extend(list(p) for p in found)
    return (list(moves) if isinstance(moves, list) else moves), info


def _search(board: chess.Board, target: str, max_depth: int, budget: int = NODE_BUDGET,
            alternatives: list | None = None):
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

    With `alternatives` (a list), every exact path found at the winning
    depth and one ply deeper is appended to it - a transposition (Bh6 Qf6
    Bg5 and Bg5 Qf6 reach the same position) is only told apart by the
    readings skipped on the way, see _pick_path.
    """
    start = _placement_of(board)
    relevant = _relevant_squares(start, target)
    # each node keeps its placement and its distance to the target: a move
    # changes 2-4 squares, so a child's are updated on those alone (building
    # and comparing all 64 squares per node was most of reconstruction's time)
    frontier = [(board.copy(stack=False), [], start, _hamming(start, target))]
    fallback: tuple[int, list[chess.Move]] | None = None
    fuzzy_routes: tuple[int, list[list[chess.Move]]] | None = None
    winner = None            # (moves, depth) once exact paths are found
    for depth in range(1, max_depth + 1):
        nxt = []
        exact = []                        # paths reaching hamming == 0
        fuzzy = []                         # (hamming, path), 0 < hamming <= FUZZ
        last = depth == max_depth          # nodes found now are never expanded
        for b, path, pl, dist in frontier:
            for mv in b.legal_moves:
                if mv.from_square not in relevant and mv.to_square not in relevant:
                    continue               # can't help - prune (see _relevant_squares)
                budget -= 1
                if budget <= 0:
                    if winner is not None:
                        return winner     # the extra depth was only for alternatives
                    if fallback:
                        return fallback[1], fallback[0]
                    return None, "budget"
                changed = _changed_squares(b, mv)
                b.push(mv)
                ham, cpl = dist, pl
                for sq in changed:
                    i = sq ^ 56
                    pc = b.piece_at(sq)
                    ch = pc.symbol() if pc else "."
                    ham += (ch != target[i]) - (pl[i] != target[i])
                    if not last:
                        cpl = cpl[:i] + ch + cpl[i + 1:]
                if ham == 0:
                    exact.append(path + [mv])
                else:
                    if ham <= FUZZ:
                        fuzzy.append((ham, path + [mv]))
                    # even a fuzzy match keeps going: it's only a fallback
                    # candidate, and a deeper exact match must still win if
                    # there is one (see docstring)
                    nxt.append((None if last else b.copy(stack=False), path + [mv], cpl, ham))
                b.pop()
        if exact:
            exact = [p for p in exact if not _has_wasted_round_trip(board, p)]
        if exact and alternatives is not None:
            alternatives.extend(exact)
        if winner is not None:
            return winner                 # the extra depth was only for alternatives
        if exact and fallback is not None and depth >= fallback[0] + 2:
            # An exact match two or more plies deeper than a near-exact one
            # is a story spun around a misread square (traced: "Ne7 cxd5
            # Nxd5 Bg2 Nf6" for Nf6 Bg2 dxc4 with the capturing pawn not
            # yet drawn). One ply deeper is still believed, see the docstring.
            return fallback[1], fallback[0]
        if exact and fuzzy_routes is not None and depth >= fuzzy_routes[0] + 2 and alternatives is not None:
            alternatives[:] = fuzzy_routes[1]      # ...likewise when the near-exact routes transpose
            return None, "fuzzy-ambiguous"
        if exact:
            if len(exact) == 1:
                winner = exact[0], depth
            else:
                shared = _common_prefix(exact)
                if shared:
                    winner = shared, depth    # only the moves every candidate agrees on
                else:
                    return None, "ambiguous"
            if alternatives is None or depth == max_depth or not nxt:
                return winner
            budget = min(budget, NODE_BUDGET // 10)   # the alternatives are a cheap extra
            frontier = nxt
            continue
        if fuzzy and fallback is None:
            best = min(h for h, _ in fuzzy)
            cand = [p for h, p in fuzzy if h == best]
            shared = cand[0] if len(cand) == 1 else _common_prefix(cand)
            if shared:
                fallback = (depth, shared)
            elif fuzzy_routes is None:
                fuzzy_routes = (depth, cand)   # near-exact routes with nothing in common
        frontier = nxt
        if not frontier:
            break
    if winner is not None:
        return winner
    if fallback:
        return fallback[1], fallback[0]
    if fuzzy_routes is not None and alternatives is not None:
        # the same moves in different orders, each a square off (a frame
        # caught mid-capture): the caller may guess the order
        alternatives[:] = fuzzy_routes[1]
        return None, "fuzzy-ambiguous"
    return None, "none"


def _single_ply_to(board: chess.Board, target: str, tolerance: int) -> list[chess.Move]:
    """Legal moves from *board* landing within *tolerance* squares of *target*."""
    out = []
    for mv in board.legal_moves:
        board.push(mv)
        if _hamming(_placement_of(board), target) <= tolerance:
            out.append(mv)
        board.pop()
    return out


def _with_lifted_king(board: chess.Board, target: str) -> str | None:
    """*target* with a missing king put back on its current square, or None
    if nothing is missing.

    A piece being dragged is drawn nowhere, and a player can hold a piece
    for seconds while the opponent moves (a premove by drag - confirmed on a
    Titled Tuesday stream: the king vanished from e1 for six seconds while
    Black played on, and every reading in between failed the king count in
    _reachable until the game was lost). Any other lifted piece just reads
    as a capture-less disappearance and is skipped harmlessly; a missing
    king is the one case that blocks the search outright."""
    cur = _placement_of(board)
    out = list(target)
    changed = False
    for king in "Kk":
        if king in target:
            continue
        sq = cur.find(king)
        if sq >= 0 and target[sq] == ".":
            out[sq] = king
            changed = True
    return "".join(out) if changed else None


def _castle_with_hidden_king(board: chess.Board, target: str) -> str | None:
    """*target* with its missing king put where a legal castling move puts
    it, when that castling explains *target* exactly apart from the king; or
    None.

    _with_lifted_king assumes a missing king stayed put, which turns a
    castling into a rook move: on Aig9-ZyQ8Ak (the king was never read)
    ...O-O read as an exact ...Rf8 and was trusted, 4 wrong plies in a row."""
    for mv in board.legal_moves:
        if not board.is_castling(mv):
            continue
        board.push(mv)
        after = _placement_of(board)
        board.pop()
        king = "K" if board.turn == chess.WHITE else "k"
        sq = after.find(king)
        if king not in target and target[sq] == "." and after[:sq] + "." + after[sq + 1:] == target:
            return after
    return None


def _same_moves(paths: list[list[chess.Move]]) -> bool:
    """Do these routes play the same moves in different orders? A burst of
    premoves (1.c4 e5 2.Nc3 Bb4 3.Nd5 ... four plies in two seconds) is
    usually seen only as its result, and the orders are the only doubt."""
    first = sorted(mv.uci() for mv in paths[0])
    return len(first) <= MAX_DEPTH and all(sorted(mv.uci() for mv in p) == first for p in paths[1:])


def _pick_path(board: chess.Board, paths: list[list[chess.Move]], skipped: list[str]) -> list[chess.Move] | None:
    """Among exact explanations of one reading, the one whose positions
    along the way best match the readings skipped since the last accepted
    one; None when the readings don't prefer any.

    The shortest legal path to a position isn't always the one played:
    Bh6 Qf6 Bg5 reads, from the positions alone, just like Bg5 Qf6, and a
    king walking g7-f6-e5 around a knight check like Ke5 straight away
    (both traced on real games, both recorded wrongly). But the frames in
    between - a square or two off the real intermediate positions, so
    skipped - do tell them apart. Each skipped reading votes for the path
    with an intermediate position nearest to it."""
    # Only readings that moved on from the current position by more than
    # one move's worth count: a frame caught mid-animation shows the piece
    # part way (a pawn passing g3 on its way to g4 reads exactly like "g3"
    # played) and would vote for a detour that never happened.
    cur = _placement_of(board)
    evidence = [r for r in skipped if _hamming(r, cur) >= 3]
    if len(paths) < 2 or not evidence:
        return None
    scores = []
    for path in paths:
        b = board.copy(stack=False)
        along = []                 # every position on the way, the final one included:
        for mv in path:            # a noisy early frame of the target itself must
            b.push(mv)             # count for the direct route too
            along.append(_placement_of(b))
        scores.append(sum(min(_hamming(r, m) for m in along) for r in evidence))
    ranked = sorted(zip(scores, [len(p) for p in paths], range(len(paths))))
    best, second = ranked[0], ranked[1]
    # a clear preference, and the readings must actually fit the winner (a
    # square off each at most) - a premove display or a cancelled move in
    # the skipped readings fits neither route and must not decide
    if best[0] > len(evidence) or second[0] - best[0] < 2:
        return None
    return paths[best[2]]


def _without_premove(board: chess.Board, target: str) -> list[tuple[str, str]]:
    """Ways to read *target* as a position plus a premove drawn on it:
    [(target with the premoved piece put back, premove uci), ...].

    A premove can sit on top of a real move - the player moves, then
    premoves at once while the opponent thinks (traced on a stream: 4.e3
    with Nbd2 premoved on the same frame, then 5.Nbd2 with exd4 premoved).
    Any piece that left a square for one where it isn't a legal arrival is
    a candidate; the caller keeps a candidate only if the rest explains
    exactly."""
    cur = _placement_of(board)
    out = []
    for a in range(64):
        piece = cur[a]
        if piece == "." or target[a] != ".":
            continue
        for b_ in range(64):
            if b_ == a or target[b_] != piece or cur[b_] == piece:
                continue
            probe = board.copy(stack=False)
            probe.turn = piece.isupper()        # the premover's own turn, to test the geometry
            mv = chess.Move(_square_of(a), _square_of(b_))
            if mv in probe.pseudo_legal_moves or _square_of(b_) in probe.attacks(_square_of(a)):
                fixed = list(target)
                fixed[a], fixed[b_] = piece, cur[b_]
                out.append(("".join(fixed), mv.uci()))
    return out


def _lands_exactly(board: chess.Board, moves: list[chess.Move], target: str) -> bool:
    b = board.copy(stack=False)
    for mv in moves:
        b.push(mv)
    return _placement_of(b) == target


def _explains_next(board: chess.Board, moves: list[chess.Move], following: str) -> bool:
    """Can the next reading be reached from the position after *moves*
    (directly, by legal moves, or with a premove drawn on it)?"""
    b = board.copy(stack=False)
    for mv in moves:
        b.push(mv)
    here = _placement_of(b)
    if following == here:
        return True
    if _hamming(here, following) > LOOKAHEAD_MAX_DIFF:
        return False      # too far to be the next move or two - and too costly to search
    # (bounded: this runs for most readings, and every premove-stripped
    # variant of a noisy frame searched to full depth once took hours)
    targets = [following] + [fixed for fixed, _ in _without_premove(b, following)][:LOOKAHEAD_MAX_VARIANTS]
    for tgt in targets:
        if tgt == here:
            return True
        if not _reachable(here, tgt):
            continue
        seq, _ = _find_sequence(b, tgt, LOOKAHEAD_DEPTH, NODE_BUDGET // 50)
        if seq is not None and _lands_exactly(b, seq, tgt):
            return True
    return False


def _board_after(moves_san: list[str]) -> chess.Board:
    b = chess.Board()
    for san in moves_san:
        b.push_san(san)
    return b


def _recover(moves_san: list[str], history: list[str], target: str, following: str | None,
             flipped: bool) -> tuple[int, list[chess.Move], chess.Board] | None:
    """(k, moves, board after k plies): a new line from ply k, replacing the
    recorded plies from there, that reaches *target* exactly and from which
    the next reading follows too; or None. Tried first: one recent piece's
    two moves merged into one; then the fewest plies to take back so that
    *target* follows by a route starting with a different move. See RECOVER."""
    if following is None:
        return None
    nxt = following[::-1] if flipped else following
    if _explains_next(_board_after(moves_san), [], nxt):
        return None      # a frame caught mid-move: the game goes on from here (traced)

    def fits_next(board: chess.Board, seq: list[chess.Move]) -> bool:
        # the next reading may be a square off (noise) as well as a move on
        b = board.copy(stack=False)
        for mv in seq:
            b.push(mv)
        return _hamming(_placement_of(b), nxt) <= FUZZ or _explains_next(b, [], nxt)

    n = len(moves_san)
    boards = [chess.Board()]
    played = []
    for san in moves_san:
        b = boards[-1].copy(stack=False)
        played.append(b.parse_san(san))
        b.push(played[-1])
        boards.append(b)
    # A piece moved twice by one side around an opponent's move was one move
    # dragged through a square on the way (traced: 21.Qe2 Rfc8 22.Qc2 for
    # 21.Qc2 Rfc8): the same position with the other side to move, so the
    # next move can't be found from it. Try the direct move instead.
    for i in range(max(0, n - RECOVER_PLIES), n - 2):
        a, b2 = played[i], played[i + 2]
        direct = chess.Move(a.from_square, b2.to_square)
        if a.to_square != b2.from_square or direct not in boards[i].legal_moves:
            continue
        line = [direct, played[i + 1]] + played[i + 3:]
        b = boards[i].copy(stack=False)
        if not all(b.is_legal(mv) and (b.push(mv) or True) for mv in line):
            continue
        here = _placement_of(b)
        if here == target:
            if fits_next(b, []):
                return i, line, boards[i]
            continue
        if not _reachable(here, target):
            continue
        seq, _ = _find_sequence(b, target, MAX_DEPTH, NODE_BUDGET // 10)
        if seq and _lands_exactly(b, seq, target) and fits_next(b, seq):
            return i, line + seq, boards[i]
    for back in range(1, RECOVER_PLIES + 1):
        k = n - back
        if k < 0:
            break
        if history[k] == target or not _reachable(history[k], target):
            continue
        start = boards[k]
        seq, _ = _find_sequence(start, target, min(MAX_DEPTH, back + 2), NODE_BUDGET // 20)
        if not seq or not _lands_exactly(start, seq, target) or start.san(seq[0]) == moves_san[k]:
            continue
        if fits_next(start, seq):
            return k, seq, start
    return None


def premove_is_white(premove: str, cur: str) -> bool:
    """Colour of the premoved piece (by its origin square in `cur`)."""
    sq = chess.parse_square(premove[:2])
    i = (7 - chess.square_rank(sq)) * 8 + chess.square_file(sq)
    return cur[i].isupper()


def _premove_display(board: chess.Board, target: str) -> str | None:
    """If *target* is the current position with one piece of the side NOT to
    move displaced, the name of that premove (e.g. "a1d1"), else None.

    chess.com and lichess draw a premove as if it were made: the piece sits
    on its destination (covering whatever was there) and its origin is
    empty, for as long as the opponent thinks. That picture is one legal
    sequence away from the real position more often than you'd hope -
    traced on a real game: White premoved Rxd1 expecting ...Qxd1+, the
    board showed a rook on d1 and a1 empty for 23 seconds, and "5...Qxd1+
    6.Rxd1" went into the record while the opponent played 5...Qa5. The
    premoving side is the one *not* on move, which no legal sequence can
    start with, so the pattern is cheap to recognise and safe to wait out."""
    cur = _placement_of(board)
    diff = [i for i in range(64) if cur[i] != target[i]]
    if len(diff) != 2:
        return None
    mover_white = not board.turn            # the side not on move
    for a in diff:
        b_ = diff[1] if a == diff[0] else diff[0]
        piece = cur[a]
        if piece == "." or piece.isupper() != mover_white or target[a] != "." or target[b_] != piece:
            continue
        probe = board.copy(stack=False)
        probe.turn = not probe.turn
        mv = chess.Move(_square_of(a), _square_of(b_))
        # the premove only has to fit the piece's movement: the sites let you
        # premove onto your own piece (the real case: Rxd1 over the queen)
        if mv in probe.pseudo_legal_moves or _square_of(b_) in probe.attacks(_square_of(a)):
            return mv.uci()
    return None


def _distinct_states(readings: list[tuple[float, str | None]], raw: dict | None = None):
    """Collapse repeats; yield ('state', ts, placement) and ('gap', ts, None).
    With `raw` (see RAW_FALLBACK), a repeat whose raw frame changed is kept:
    the raw frame may say what the vote hid."""
    out = []
    last = None
    last_raw = None
    missing = 0
    for ts, placement in readings:
        if placement is None:
            missing += 1
            if missing == GAP_FRAMES:
                out.append(("gap", ts, None))
                last = None
            continue
        missing = 0
        now_raw = raw.get(ts) if raw else None
        if placement != last or (now_raw and now_raw != last_raw and now_raw != placement):
            out.append(("state", ts, placement))
            last = placement
        last_raw = now_raw
    return out


SETTLE_TAIL_S = 60.0   # how long after a game's last ply its position may still be read
# An exact raw frame settles a ply too: the vote can blend it away. Whole
# library: read to the end 867 -> 877, right plies +66, trusted +26, no game
# worse. Also settling on an exact reading up to SETTLE_AFTER_S after the ply,
# past the next one (the narrator stepping back to it): at 30 s no more games
# read to the end, and 5 lose "every move right" (0 = off).
SETTLE_RAW = int(os.environ.get("RECON_SETTLE_RAW", "1"))
SETTLE_AFTER_S = float(os.environ.get("RECON_SETTLE_AFTER", "0"))
# A-B switch (0 = off): a reading that differs from the ply's position only in
# one of its pieces read as an empty square, away from the move's squares,
# settles it too - something on screen covering that square (seen: 22.Qxc7
# with c7 read empty for the 1.2 s before the narrator stepped back). Among
# the plies the drop cut, that difference was right 232 : 46 wrong; a piece
# read where the position has none was mostly wrong. SETTLE_COVERED_FRAMES
# caps how many frames may show the difference (0 = any).
SETTLE_COVERED = int(os.environ.get("RECON_SETTLE_COVERED", "0"))
SETTLE_COVERED_FRAMES = int(os.environ.get("RECON_SETTLE_COVERED_FRAMES", "0"))


def _covered(here: str, read: str, keep: set[int]) -> bool:
    """`read` is `here` with one piece, on a square not in `keep`, read as empty."""
    diff = [k for k in range(64) if here[k] != read[k]]
    return len(diff) == 1 and diff[0] not in keep and read[diff[0]] == "." and here[diff[0]] != "."


def _settled(game: "Game", readings: list[tuple[float, str | None]],
             raw: dict[float, str | None] | None = None, near: list[str] | None = None) -> list[bool]:
    """Game.settled, from the (voted) readings; fills `near` (Game.settle_near) when given."""
    if SETTLE_RAW and raw:
        readings = sorted(list(readings) + [(t, p) for t, p in raw.items() if p is not None], key=lambda r: r[0])
    times = [ts for ts, _ in readings]
    board = chess.Board()
    out = []
    for i, san in enumerate(game.moves_san):
        mv = board.parse_san(san)
        board.push(mv)
        here = _placement_of(board)
        # placement index of a square: rank 8 first (see _placement_of)
        keep = {(7 - chess.square_rank(q)) * 8 + chess.square_file(q) for q in (mv.from_square, mv.to_square)}
        if game.flipped:
            here = here[::-1]
            keep = {63 - k for k in keep}
        t0 = game.move_seconds[i]
        t1 = game.move_seconds[i + 1] if i + 1 < len(game.move_seconds) else t0 + SETTLE_TAIL_S
        t1 = max(t1, t0 + SETTLE_AFTER_S)
        lo, hi = bisect.bisect_right(times, t0), bisect.bisect_right(times, t1)
        ok = any(readings[j][1] == here for j in range(lo, hi))
        if not ok and SETTLE_COVERED:
            cov = [j for j in range(lo, hi) if readings[j][1] is not None and _covered(here, readings[j][1], keep)]
            close = sum(readings[j][1] is not None and _hamming(readings[j][1], here) == 1 for j in range(lo, hi))
            ok = bool(cov) and (not SETTLE_COVERED_FRAMES or close <= SETTLE_COVERED_FRAMES)
        out.append(ok)
        if near is not None:
            seen = [readings[j][1] for j in range(lo, hi) if readings[j][1] is not None]
            best = min((_hamming(p, here) for p in seen), default=None)
            if best is None:
                near.append("none")
            else:
                p = next(p for p in seen if _hamming(p, here) == best)
                sq = [k for k in range(64) if p[k] != here[k]]
                if game.flipped:
                    sq = [63 - k for k in sq]
                names = ",".join(f"{chess.square_name(chess.square(k % 8, 7 - k // 8))}"
                                 f"{here[63 - k if game.flipped else k]}>{p[63 - k if game.flipped else k]}" for k in sq[:4])
                near.append(f"d{best}x{sum(_hamming(p, here) == best for p in seen)}:{names}")
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


def _went_elsewhere(pending, target: str) -> bool:
    """For RAW_CONFIRM: does `target`, a reading nothing explained right
    after a raw frame's moves, fit better with the first of them going to
    another square (a piece caught on its way)? Always yes for RAW_CONFIRM 1."""
    if RAW_CONFIRM != 2:
        return True
    _, _, before, moves = pending
    via = before.copy(stack=False)
    for mv in moves:
        via.push(mv)
    off = _hamming(_placement_of(via), target)
    first = moves[0]
    for mv in before.legal_moves:
        if mv.from_square == first.from_square and mv.to_square != first.to_square:
            alt = before.copy(stack=False)
            alt.push(mv)
            if _hamming(_placement_of(alt), target) < off:
                return True
    return False


def _shared_opening(g: Game, earlier: list[Game]) -> int:
    """The most opening plies g shares with any earlier game."""
    shared = 0
    for h in earlier:
        n = 0
        for a, b in zip(h.moves_san, g.moves_san):
            if a != b:
                break
            n += 1
        shared = max(shared, n)
    return shared


def is_frozen_replay(g: Game, earlier: list[Game]) -> bool:
    """A review the narrator steps through slowly escapes _is_replay, and may
    branch into a variation (seen: 19 plies trusted, 8 of them not played).
    Its clocks give it away: they were read but never ran, while every real
    game's clocks do. Among 2,262 games of a full rescan, 32 had frozen clocks;
    the ones sharing 10+ opening plies with an earlier game were all copies of
    it, the real games among the rest shared at most 8. Needs clocks read
    (scan.annotate_clocks), so it runs after reconstruct()."""
    if not REPLAY_FROZEN:
        return False
    sides = [[c for c in s if c is not None] for s in (g.clock_white_seconds, g.clock_black_seconds)]
    read = [s for s in sides if s]
    if sum(map(len, read)) < 3 or any(max(s) - min(s) > 1 for s in read):
        return False
    return _shared_opening(g, earlier) >= REPLAY_SHARED


def _is_replay(g: Game, earlier: list[Game]) -> bool:
    """After a game the narrator often steps back through it, several plies a
    second. At 1 fps that never formed a game, but in a stretch read again at
    refine.STALL_FPS it does: a second copy of the game, partly misread (seen:
    14 wrong plies trusted). Neither sign is enough alone: real games in one
    video share up to 20 opening plies, and read at 6 fps a premoved opening
    reaches 18 plies in 4 s. Together they were clean on 2,600 real games:
    those sharing 10+ plies peaked at 11 plies in 4 s, the replays 17 to 29."""
    if not REPLAY_FAST or _shared_opening(g, earlier) < REPLAY_SHARED:
        return False
    ts, j = g.move_seconds, 0
    for i in range(len(ts)):
        while ts[i] - ts[j] > REPLAY_WINDOW_S:
            j += 1
        if i - j + 1 >= REPLAY_FAST:
            return True
    return False


def reconstruct(readings: list[tuple[float, str | None]],
                 clock_by_side: dict[str, list[tuple[float, float]]] | None = None,
                 trace=None, raw: dict[float, str | None] | None = None) -> list[Game]:
    """`trace(ts, message)`, if given, is told what happened at each reading
    (games opened and closed, moves taken, readings skipped and why) - for
    debugging a video, see tests/trace_video.py. `raw`, if given, maps each
    reading's time to the frame as read before recognize.vote_stream: see
    RAW_FALLBACK."""
    events = _distinct_states(readings, raw if RAW_FALLBACK else None)
    note = trace or (lambda ts, msg: None)
    games: list[Game] = []
    current: Game | None = None
    board: chess.Board | None = None
    last_ts = 0.0

    # Placements after each ply of the current game (history[i] = after i
    # plies), to recognise the viewer browsing back through the move list
    # mid-game - see the browsing check in the loop below.
    history: list[str] = []
    browsing = False
    skipped: list[str] = []        # readings stepped over since the last accepted one (see _pick_path)
    pending_fuzzy: list[int] = []  # plies of the last bunch accepted a square off, awaiting an exact sighting
    pending_seen = 0.0             # ...seconds its resulting position has been read exactly since
    undone_from: int | None = None  # ply count the board just reverted to (see the undo check)
    after_retract = False          # an undo / browse back / detour since the last accepted ply
    after_unreach1 = False         # an unexplainable reading a square off since the last accepted ply
    unreach1_ply = None            # ...and the ply count at the last one (see BRIEF_UNDO_NEAR)
    brief_undo = False             # a brief undo that didn't end trust since the last accepted ply
    last_retract = -1e9            # when the board last went back (undo, browsing, detour): see RECOVER

    def close(why: str = ""):
        nonlocal current, board, history, browsing, skipped, undone_from, pending_fuzzy, pending_seen, after_retract, after_unreach1, unreach1_ply, brief_undo
        if current:
            note(last_ts, f"close {why}: {current.ply} plies, {current.skipped} skipped")
        if current and current.ply >= 2:
            games.append(current)
        current, board = None, None
        history, browsing, skipped, undone_from = [], False, [], None
        pending_fuzzy, pending_seen = [], 0.0
        after_retract = after_unreach1 = brief_undo = False
        unreach1_ply = None

    def retract_to(k: int) -> None:
        nonlocal board
        del current.moves_san[k:], current.move_seconds[k:], history[k + 1:]
        current.ambiguous = [i for i in current.ambiguous if i < k]
        current.fuzzy = [i for i in current.fuzzy if i < k]
        current.doubtful = [i for i in current.doubtful if i < k]
        current.rough = [i for i in current.rough if i < k]
        board = chess.Board()
        for san in current.moves_san:
            board.push_san(san)

    def next_state(i: int):
        """The next readable board after events[i], if it's soon enough
        (repeats kept for their raw frame, see _distinct_states, don't count)."""
        seen = 0
        for kind2, ts2, placement2 in events[i + 1:]:
            if kind2 != "state" or ts2 - events[i][1] > LOOKAHEAD_S:
                return None
            if placement2 != events[i][2]:
                return placement2
            seen += 1
            if seen >= 2:
                return None
        return None

    misses = 0
    ei = -1
    raw_tried = -1    # the event last tried again as its raw frame
    raw_unreach1 = False   # ...and its voted reading was unreachable, a square off
    raw_split = None       # (first ply, plies, time) of the last ply taken from a raw frame
    raw_pending = None     # (game, plies, board before, moves) taken from a raw frame, awaiting a reading that follows
    redo = 0      # re-examine the same reading from the new position (see the partial check)
    while ei + 1 < len(events) or redo:
        if redo:
            redo -= 1
        else:
            ei += 1
        kind, ts, placement = events[ei]
        if kind == "gap":
            close("gap")
            continue

        orient = _is_start(placement)
        if orient is not None:
            close("new start")
            note(ts, f"start ({orient})")
            current = Game(start_seconds=ts, flipped=(orient == "flipped"))
            board = chess.Board()
            history = [_placement_of(board)]
            misses = 0
            last_ts = ts
            continue

        if REOPEN_OTHER_SIDE and current is not None and board is not None:
            current_flipped = current.flipped
            tgt = placement if current.flipped else placement[::-1]   # the other way up
            if _hamming(tgt, START) <= OPEN_HAMMING and \
                    _hamming(_placement_of(board), placement[::-1] if current.flipped else placement) >= REOPEN_FAR \
                    and (seq := _find_sequence(chess.Board(), tgt, REOPEN_DEPTH)[0]):
                close("new game, the board turned")
                current = Game(start_seconds=ts, flipped=not current_flipped)
                board = chess.Board()
                history = [_placement_of(board)]
                for mv in seq:
                    current.moves_san.append(board.san(mv))
                    current.move_seconds.append(ts)
                    board.push(mv)
                    history.append(_placement_of(board))
                misses = 0
                last_ts = ts
                note(ts, f"open mid-game ({'flipped' if current.flipped else 'normal'}) with {' '.join(current.moves_san)}")
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
                        note(ts, f"open mid-game ({'flipped' if flip else 'normal'}) with {' '.join(current.moves_san)}")
                        break
            continue

        target = placement[::-1] if current.flipped else placement
        cur_placement = _placement_of(board)
        repaired = _with_lifted_king(board, target)
        hidden_kings = ""
        if repaired is not None:
            hidden_kings = "".join(k for k in "Kk" if k not in target) if HIDDEN_KING else ""
            castled = _castle_with_hidden_king(board, target) if HIDDEN_KING else None
            if castled is not None:
                note(ts, "king missing: castling explains the rest")
                target = castled
            else:
                note(ts, "king lifted: assumed still on its square")
                target = repaired
        if target == cur_placement:
            misses = 0
            browsing = False
            skipped = []
            undone_from = None
            if pending_fuzzy and FUZZY_CONFIRM_S:
                # the bunch's resulting position read exactly: on 209
                # benchmarked games every wrong fuzzy bunch's position was
                # never read exactly before the next move, while 24 right
                # ones were, for 2+ seconds
                nxt_ts = events[ei + 1][1] if ei + 1 < len(events) else ts + 1.0
                pending_seen += max(1.0, nxt_ts - ts)
                if pending_seen >= FUZZY_CONFIRM_S:
                    note(ts, f"fuzzy bunch at plies {pending_fuzzy[0] + 1}-{pending_fuzzy[-1] + 1} confirmed")
                    current.fuzzy = [i for i in current.fuzzy if i not in set(pending_fuzzy)]
                    pending_fuzzy, pending_seen = [], 0.0
            continue

        # A move shown and taken back: the board reverted to the position
        # before the last ply (a cancelled premove, a takeback, or a piece
        # dragged and dropped back), and now shows a *different* move from
        # there. Confirmed on a real game: 38.c4 appeared for three seconds,
        # the board reverted, then 38.Bg6 was played - and c4 stayed in the
        # record while Bg6 could no longer be explained. Retract the ply and
        # take the new one instead.
        if UNDO and undone_from is not None and undone_from == len(history) - 2:
            prev = chess.Board()
            for san in current.moves_san[:-1]:
                prev.push_san(san)
            undo_seq = None
            if _reachable(_placement_of(prev), target):
                seq, _ = _find_sequence(prev, target, MAX_DEPTH)
                if seq is not None and _lands_exactly(prev, seq, target):
                    undo_seq = seq
            if undo_seq:
                note(ts, f"undo: ply {current.ply} {current.moves_san[-1]} taken back")
                last_retract = ts
                retract_to(len(history) - 2)
                # a bunch of its own, for tests/trust_replay.py
                note(ts, f"ply {current.ply + 1}: " + " ".join(board.variation_san(undo_seq).split()[-len(undo_seq) * 2:]))
                if RETRACT_ENDS_TRUST:
                    current.doubtful.extend(range(current.ply, current.ply + len(undo_seq)))
                for mv in undo_seq:
                    current.moves_san.append(board.san(mv))
                    current.move_seconds.append(ts)
                    board.push(mv)
                    history.append(_placement_of(board))
                if len(undo_seq) == 1:
                    current.clean += 1
                else:
                    current.bridged += 1
                misses, last_ts, browsing, skipped, undone_from = 0, ts, False, [], None
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
        if BUNCH_UNDO and earlier and not browsing and earlier[-1] < n - 1 and ts - current.move_seconds[-1] <= UNDO_WINDOW_S \
                and len(set(current.move_seconds[earlier[-1]:n])) == 1:
            # Several plies accepted from one reading, then the board went
            # straight back to before them: the reading was a piece in hand
            # (traced: a rook dragged for two seconds read as "Ra3 a4 Ra1
            # a3"; the revert looked like browsing, the four plies stayed,
            # and 65 correct plies were lost after them).
            k = earlier[-1]
            note(ts, f"undo: plies {k + 1}-{n} ({' '.join(current.moves_san[k:n])}) shown briefly, taken back")
            retract_to(k)
            after_retract = True
            last_retract = ts
            misses = 0
            continue
        if earlier and (browsing or earlier[-1] == n - 1 or (n - earlier[-1]) % 2 == 0):
            if earlier[-1] == n - 1 and not browsing and ts - current.move_seconds[-1] <= UNDO_WINDOW_S:
                # the last ply was on screen only briefly before the board
                # reverted: a move dragged and dropped back, or a cancelled
                # premove (traced: "Rf4" shown for two seconds, then the
                # board sat on the old position for ten). Not browsing.
                note(ts, f"undo: ply {current.ply} {current.moves_san[-1]} shown briefly, taken back")
                # Naroditsky drags pieces to show ideas while explaining:
                # such an undo ends trust only near a reading a square off
                near = unreach1_ply is not None and current.ply - unreach1_ply <= BRIEF_UNDO_NEAR
                retract_to(n - 1)
                after_retract = after_retract or not BRIEF_UNDO_NEAR or near
                brief_undo = not after_retract
                last_retract = ts
                misses = 0
                continue
            for _ in range(BROWSE_STEP_PLIES if BROWSE_STEP_S else 0):
                if browsing or earlier[-1] >= n - 1 or ts - current.move_seconds[-1] > BROWSE_STEP_S \
                        or _hamming(history[n], target) >= _hamming(history[n - 1], target):
                    break
                # the last ply took the board part of the way to the position the
                # viewer browsed back to: a frame caught between the two, not a
                # move (traced: 9.Qe4 Nxf4, then browsing back to before 9.Qe4
                # read as "10.Qa4" - the queen back on a4 - a second earlier)
                note(ts, f"browsing back: ply {current.ply} {current.moves_san[-1]} was a step towards it")
                retract_to(n - 1)
                n -= 1
            browsing = True
            last_retract = ts
            if earlier[-1] == n - 1:
                undone_from = n - 1
            note(ts, f"browsing back to ply {earlier[-1]}")
            after_retract = after_retract or RETRACT_BROWSE
            continue

        premove = _premove_display(board, target)
        if premove is not None:
            # not a move, not noise either - the opponent is still thinking
            note(ts, f"premove shown ({premove})")
            if raw_pending is not None and raw_pending[0] is current and _went_elsewhere(raw_pending, target):
                current.doubtful.extend(raw_pending[1])
            raw_pending = None
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
            # Once a few readings in a row couldn't be explained the game is
            # most likely lost (or the board is being misread the same way
            # every frame), and burning the full budget on every further
            # reading made one blitz stream take an hour to reconstruct.
            budget = NODE_BUDGET if misses < 2 else NODE_BUDGET // 4 if misses < 5 else NODE_BUDGET // 20
            alternatives: list = []
            moves, info = _find_sequence(board, target, MAX_DEPTH, budget, alternatives)
            guessed = False
            guess_span = 0      # plies marked ambiguous beyond the moves taken now
            if (moves is None or not _lands_exactly(board, moves, target)) \
                    and _hamming(cur_placement, target) <= PREMOVE_MAX_DIFF:
                # not explained exactly: maybe a premove is drawn on top of
                # the real move(s) - put it back and try again. "4.e3 with
                # Nbd2 premoved" and "4.Nbd2 with e3 premoved" both fit
                # such a frame; the next reading tells them apart.
                options = []
                for fixed, premove in _without_premove(board, target):
                    if fixed == cur_placement or not _reachable(cur_placement, fixed):
                        continue
                    alt_alternatives: list = []
                    # one real ply only: the premover's own move, premoved on at
                    # once. Allowing more let a noisy frame of "Nxc5+ Kb6" be
                    # read as "Nxd4 cxd4 with b6 premoved" and lose the game.
                    alt, alt_info = _find_sequence(board, fixed, 1, min(budget, NODE_BUDGET // 10), alt_alternatives)
                    if alt is not None and _lands_exactly(board, alt, fixed):
                        mover_white = (board.turn == chess.WHITE) == (len(alt) % 2 == 1)
                        if mover_white == premove_is_white(premove, cur_placement):
                            options.append((alt, alt_info, alt_alternatives, fixed, premove))
                if len(options) > 1:
                    following = next_state(ei)
                    if following is not None:
                        nxt_tgt = following[::-1] if current.flipped else following
                        fits = [o for o in options if _explains_next(board, o[0], nxt_tgt)]
                        if fits:
                            options = fits
                if options:
                    alt, alt_info, alt_alternatives, fixed, premove = options[0]
                    note(ts, f"premove {premove} drawn on top of the move(s)"
                             + (f" ({len(options)} readings possible)" if len(options) > 1 else ""))
                    moves, info, alternatives = alt, alt_info, alt_alternatives
                    target = fixed
                    if len(options) > 1:
                        # which piece was the premove is a guess, so the order
                        # of this move and the premove's later execution is
                        # too: a transposition window, recorded as such
                        guessed, guess_span = True, 2
            if moves is not None and len(alternatives) > 1:
                chosen = _pick_path(board, alternatives, skipped)
                if chosen is not None and chosen != moves:
                    note(ts, f"skipped readings prefer {board.variation_san(chosen)} over {board.variation_san(moves)}")
                    moves = chosen
            elif moves is None and len(alternatives) >= 2 and (
                    (info == "ambiguous" and len(alternatives) <= GUESS_MAX_ROUTES
                     and max(len(p) for p in alternatives) <= GUESS_MAX_PLIES)
                    or (info in ("ambiguous", "fuzzy-ambiguous") and _same_moves(alternatives))):
                # Several routes, one destination (a burst of premoves: the
                # knight reached e4 via d2 or c3, nobody saw which). The
                # position is certain, so the game goes on from it; the
                # route is recorded as a guess that trust.py never shows.
                moves = _pick_path(board, alternatives, skipped) or min(alternatives, key=len)
                guessed = True
                note(ts, f"ambiguous route, took {board.variation_san(moves)} of {len(alternatives)}")
        else:
            moves, info, guessed, guess_span = None, "unreachable", False, 0
        if moves is not None and _clock_contradicts_bridge(clock_by_side, board, moves, last_ts, ts):
            moves, info = None, "clock-contradicted"
        # A reading explained only by several plies at once deserves a second
        # look: a frame caught mid-move (a piece lifted, a capture half
        # drawn) is a square or two off the real next position, and a
        # legal-but-spurious 2-3 ply sequence can land *exactly* on such a
        # frame, beating the true single move that lands one square off
        # (traced on real games: "Rxa3 ..." recorded for Rxb2, "Qa7 ..." for
        # Qxc4). So if the very next reading is reached from the *current*
        # position by fewer plies than this bridge needs, this reading was
        # noise: skip it and let the next one tell the story.
        partial = moves is not None and isinstance(info, int) and info > len(moves)   # a shared prefix of deeper routes
        exact_now = moves is not None and _lands_exactly(board, moves, target)
        from_raw = raw_tried == ei     # a raw frame can catch a piece mid-drag: check it like a bunch
        if LOOKAHEAD and moves is not None and not partial and (not exact_now or len(moves) >= 2 or from_raw):
            # Moves accepted a square off, or several at once: a frame caught
            # mid-capture (traced: "Bb5" for the trade Qb5 Qxb5 Bxb5 with one
            # queen already gone), or a piece in hand plus the opponent's
            # premove drawn ("Bf1 Bxf1", exactly, for Ba6 with Bf1 premoved).
            # If the next reading follows exactly from *here* but not from
            # after those moves, they weren't played.
            following = next_state(ei)
            if following is not None:
                nxt_tgt = following[::-1] if current.flipped else following
                if nxt_tgt != cur_placement and _explains_next(board, [], nxt_tgt) \
                        and not _explains_next(board, moves, nxt_tgt):
                    note(ts, f"lookahead: the next reading follows from here, not from {board.variation_san(moves)}")
                    moves, info = None, "superseded"
        if LOOKAHEAD and moves is not None and from_raw and not partial:
            # a raw frame's moves are kept only if the next reading doesn't
            # follow more directly from before them (traced: 7.Bc4, the
            # bishop caught on d3 on its way, read as "7.Bd3", and then the
            # bishop on c4 taken for a premove arrow; 19...Rxe3 20.Rxe3 with
            # the rook caught on e5, read as "19...Re5 20.Re2 Rxe3 21.Rxe3")
            following = next_state(ei)
            if following is not None:
                nxt_tgt = following[::-1] if current.flipped else following
                if nxt_tgt != cur_placement and _reachable(cur_placement, nxt_tgt):
                    via = board.copy()
                    for mv in moves:
                        via.push(mv)
                    after = None
                    if _reachable(_placement_of(via), nxt_tgt):
                        after, _ = _find_sequence(via, nxt_tgt, MAX_DEPTH, NODE_BUDGET // 4)
                        if after is not None and not _lands_exactly(via, after, nxt_tgt):
                            after = None
                    depth = len(moves) + len(after) - 1 if after else len(moves)
                    routes = []
                    short, _ = _find_sequence(board, nxt_tgt, depth, NODE_BUDGET // 4, routes)
                    direct = short if short and len(short) <= depth and _lands_exactly(board, short, nxt_tgt) else \
                        next((r for r in routes if len(r) <= depth and _lands_exactly(board, r, nxt_tgt)), None)
                    if direct is not None and direct[:len(moves)] != moves:
                        note(ts, f"lookahead: the next reading is {board.variation_san(direct)} from here; "
                                 f"the raw frame's {board.variation_san(moves)} was a piece on its way")
                        moves, info = None, "superseded"
        if LOOKAHEAD and moves is not None and (len(moves) >= 2 or partial):
            following = next_state(ei)
            if following is not None:
                nxt_tgt = following[::-1] if current.flipped else following
                if nxt_tgt != cur_placement and _reachable(cur_placement, nxt_tgt):
                    routes: list = []
                    span = info if partial else len(moves)   # plies this reading claims
                    short, _ = _find_sequence(board, nxt_tgt, span - 1, NODE_BUDGET, routes)
                    slack = FUZZ if LOOKAHEAD_FUZZ else 0
                    fits = None
                    if short:
                        b2 = board.copy(stack=False)
                        for mv in short:
                            b2.push(mv)
                        if _hamming(_placement_of(b2), nxt_tgt) <= slack:
                            fits = short
                    if fits is None:
                        # several short routes (so no single answer) still
                        # mean the next reading is reachable in fewer plies
                        fits = next((r for r in routes if len(r) < span and _lands_exactly(board, r, nxt_tgt)), None)
                    if fits is not None:
                        note(ts, f"lookahead: the next reading is {board.variation_san(fits)} "
                                 f"from here, not {board.variation_san(moves)} and on")
                        moves, info = None, "superseded"
        if moves is None and UNDO and current.ply >= 1 and ts - current.move_seconds[-1] <= UNDO_WINDOW_S \
                and current.move_seconds[-1] != (current.move_seconds[-2] if current.ply >= 2 else None):
            # Unexplained right after a single ply appeared: was that ply a
            # piece dragged over a square on its way elsewhere? (Traced: the
            # c-pawn hovered over c4 for two seconds and was dropped on c3.)
            # If a different single move from the previous position explains
            # this reading exactly, the brief ply was no move at all.
            prev = chess.Board()
            for san in current.moves_san[:-1]:
                prev.push_san(san)
            cands = _single_ply_to(prev, target, 0)
            if len(cands) == 1 and prev.san(cands[0]) != current.moves_san[-1]:
                note(ts, f"undo: ply {current.ply} {current.moves_san[-1]} was a piece passing through; "
                         f"{prev.san(cands[0])} played")
                retract_to(len(history) - 2)
                moves, info = cands, "undo"
                after_retract = True
                last_retract = ts

        if moves is None and RAW_FALLBACK and raw and raw_tried != ei \
                and info not in ("superseded", "clock-contradicted"):
            # The flicker vote mixes consecutive frames square by square, so
            # when moves come every second it can blend two positions into one
            # that never existed (traced: a king walking f1-e2-d3 voted off
            # e2, while the raw frames showed each position exactly and the
            # game closed 26 plies early). Try the frame as it was read.
            alt = raw.get(ts)
            if alt and alt != placement and (alt[::-1] if current.flipped else alt) not in history:
                # (a raw frame showing an earlier position is the viewer
                # browsing back: it may add moves, never take any back -
                # traced: 24...Qe5 undone for "Qe7" mid-browse)
                note(ts, f"voted reading unexplained ({info}): trying the raw frame")
                raw_unreach1 = info == "unreachable" and _hamming(cur_placement, target) == 1
                events[ei] = ("state", ts, alt)
                raw_tried = ei
                redo += 1
                continue

        if moves is None:
            # a noisy / mid-animation reading - step over it and try to resync
            if raw_pending is not None and raw_pending[0] is current and _went_elsewhere(raw_pending, target):
                # nothing after a raw frame's moves followed from them, and
                # the piece it moved fits better on another square (traced:
                # 60...f5 61.gxf6 e.p. read as "60...f6", the pawn caught on
                # its way; then f5 was taken for a premove)
                current.doubtful.extend(raw_pending[1])
            raw_pending = None
            misses += 1
            current.skipped += 1
            skipped.append(target)
            note(ts, f"skip ({info}; {_hamming(cur_placement, target)} squares off) miss {misses}")
            if info == "unreachable" and _hamming(cur_placement, target) == 1:
                after_unreach1 = True
                unreach1_ply = current.ply
            if RECOVER and misses <= 3 and _hamming(cur_placement, target) >= RECOVER_MIN_DIFF \
                    and (RECOVER >= 2 or info not in ("ambiguous", "fuzzy-ambiguous")) \
                    and ts - last_retract > RECOVER_QUIET_S and not (RECOVER_STRICT and repaired is not None):
                # Derailed: a wrong ply was accepted a little while ago (traced:
                # "21.Qe2" for a queen dragged on its way to c2, then 22.Qc2
                # too, a tempo out, and nothing after it could be explained, so
                # the game closed 60 plies early). If this reading follows
                # exactly from a few plies back by another route, and the next
                # reading follows from there, take that route instead.
                fix = _recover(current.moves_san, history, target, next_state(ei), current.flipped)
                if fix is not None and RECOVER_STRICT and len(fix[1]) < current.ply - fix[0]:
                    # a derail leaves the game behind, so the true line is at
                    # least as long as the plies it replaces (traced: five right
                    # plies replaced by two on a frame read 3 squares off)
                    fix = None
                if fix is not None:
                    k, seq, start = fix
                    note(ts, f"recover: plies {k + 1}-{current.ply} ({' '.join(current.moves_san[k:])}) "
                             f"replaced by {start.variation_san(seq)}")
                    retract_to(k)
                    # a bunch of its own, for tests/trust_replay.py
                    note(ts, f"ply {current.ply + 1}: " + " ".join(board.variation_san(seq).split()[-len(seq) * 2:]))
                    current.doubtful.extend(range(k, k + len(seq)))
                    for mv in seq:
                        current.moves_san.append(board.san(mv))
                        current.move_seconds.append(ts)
                        board.push(mv)
                        history.append(_placement_of(board))
                    current.bridged += 1
                    misses, last_ts, browsing, skipped, undone_from = 0, ts, False, [], None
                    after_retract = after_unreach1 = False
                    pending_fuzzy, pending_seen = [], 0.0
                    continue
            if misses >= MAX_MISSES:
                close("too many misses")
            continue

        misses = 0
        last_ts = ts
        browsing = False
        skipped = []
        undone_from = None
        note(ts, f"ply {current.ply + 1}: " + " ".join(board.variation_san(moves).split()[-len(moves) * 2:]))
        if guessed:
            current.ambiguous.extend(range(current.ply, current.ply + len(moves) + guess_span))
        if (after_retract and RETRACT_ENDS_TRUST) or (after_unreach1 and UNREACH_BUNCH and len(moves) >= 2):
            current.doubtful.extend(range(current.ply, current.ply + len(moves)))
        if after_unreach1:
            current.rough.append(current.ply)
        if from_raw and (RAW_DOUBTFUL == 2 or (RAW_DOUBTFUL == 1 and raw_unreach1) or (BRIEF_UNDO_RAW and brief_undo)):
            current.doubtful.extend(range(current.ply, current.ply + len(moves)))
        if RAW_DOUBTFUL == 3 and raw_split is not None and not from_raw and ts - raw_split[2] <= RAW_SPLIT_S \
                and raw_split[1] + len(moves) >= RAW_SPLIT_BUNCH and raw_split[0] + raw_split[1] == current.ply:
            # the raw frame split a bunch the vote would have shown whole, and
            # a big bunch is what ends trust: keep it ending
            # there (traced: 3...Nc6 4.Nxc6 dxc6 split, then 8.Qe2 misread
            # and trusted)
            current.doubtful.extend(range(raw_split[0], current.ply + len(moves)))
        raw_split = (current.ply, len(moves), ts) if from_raw else None
        raw_pending = (current, range(current.ply, current.ply + len(moves)), board.copy(stack=False), moves) \
            if from_raw and RAW_CONFIRM else None
        after_retract = after_unreach1 = brief_undo = False
        if hidden_kings:
            # where a missing king really is is a guess: a move by its side
            # could be a king move or castling in disguise
            for j, mv in enumerate(moves):
                mover = "K" if (board.turn == chess.WHITE) == (j % 2 == 0) else "k"
                if mover in hidden_kings:
                    current.ambiguous.append(current.ply + j)
        pending_fuzzy, pending_seen = [], 0.0
        if len(moves) >= 2 and not _lands_exactly(board, moves, target):
            current.fuzzy.extend(range(current.ply, current.ply + len(moves)))
            pending_fuzzy = list(range(current.ply, current.ply + len(moves)))
        for mv in moves:
            current.moves_san.append(board.san(mv))
            current.move_seconds.append(ts)
            board.push(mv)
            history.append(_placement_of(board))
        if len(moves) == 1:
            current.clean += 1
        else:
            current.bridged += 1
        if isinstance(info, int) and info > len(moves) and _placement_of(board) != target:
            # only the moves every route shared were taken (a burst of
            # premoves: 3.Nd5 then Nc6-or-Na6 Nxb4 Nxb4); look at the same
            # reading again from here, where what's left may be guessable
            redo = 1

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
                note(ts, f"detour: retract to ply {back}")
                retract_to(back)
                after_retract = True
                last_retract = ts

        if board.is_checkmate() or board.is_stalemate():
            # no legal move exists from here - anything read after this point
            # cannot be a continuation of this game. Without this, a player
            # reviewing the finished game (playing out "what if" lines in
            # analysis mode, which looks like an ordinary legal continuation
            # from the board alone) would get silently grafted onto the real
            # game's move list as if it had actually been played.
            close("mate/stalemate")
            continue

        if current.ply >= MAX_PLIES:
            close("max plies")

    close("end of readings")
    kept: list[Game] = []
    for g in games:
        if _is_replay(g, kept):
            note(g.start_seconds, f"replay of an earlier game dropped ({g.ply} plies)")
            continue
        g.settled = _settled(g, readings, raw, near=g.settle_near)
        kept.append(g)
    return kept
