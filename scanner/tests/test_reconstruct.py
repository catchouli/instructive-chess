"""Feed synthetic board-reading streams to the reconstructor and check the SAN."""
import sys
from pathlib import Path

import chess
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import reconstruct  # noqa: E402
from reconstruct import _placement_of, reconstruct as run  # noqa: E402


def stream_for(sans, *, hold=2, flip=False, drop=()):
    """Build (ts, placement) readings for a game given as a SAN list.

    hold  - how many identical frames each position occupies
    flip  - emit placements as the board would read with black at the bottom
    drop  - set of ply indices (1-based) whose frames are replaced by None
    """
    board = chess.Board()
    readings = []
    ts = 0.0

    def emit(placement, n):
        nonlocal ts
        for _ in range(n):
            readings.append((ts, placement))
            ts += 1.0

    emit(_placement_of(board)[::-1] if flip else _placement_of(board), hold)
    for i, san in enumerate(sans, start=1):
        board.push_san(san)
        p = _placement_of(board)
        if flip:
            p = p[::-1]
        if i in drop:
            for _ in range(hold):
                readings.append((ts, None))
                ts += 1.0
        else:
            emit(p, hold)
    return readings


ITALIAN = ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5", "c3", "Nf6", "d4", "exd4", "cxd4", "Bb4+"]
SICILIAN = ["e4", "c5", "Nf3", "d6", "d4", "cxd4", "Nxd4", "Nf6", "Nc3", "a6"]


def test_italian_roundtrip():
    games = run(stream_for(ITALIAN))
    assert len(games) == 1
    assert games[0].moves_san == ITALIAN
    assert games[0].flipped is False
    assert games[0].confidence == pytest.approx(1.0)
    # every move should carry the video timestamp it was confirmed at
    assert len(games[0].move_seconds) == len(ITALIAN)
    assert games[0].move_seconds == sorted(games[0].move_seconds)  # non-decreasing


def test_sicilian_roundtrip():
    games = run(stream_for(SICILIAN))
    assert len(games) == 1
    assert games[0].moves_san == SICILIAN


def test_castling_and_promotion():
    sans = ["e4", "e5", "Nf3", "Nc6", "Bc4", "Nf6", "O-O", "Bc5", "d3", "d6"]
    games = run(stream_for(sans))
    assert games[0].moves_san == sans


def test_flipped_board_is_detected():
    games = run(stream_for(ITALIAN, flip=True))
    assert len(games) == 1
    assert games[0].flipped is True
    assert games[0].moves_san == ITALIAN


def test_single_frame_gap_is_bridged():
    # drop the frame after ply 4 (Nc6); reconstructor must bridge 3.Nf3->..Nc6
    games = run(stream_for(ITALIAN, drop=(4,)))
    assert games[0].moves_san == ITALIAN
    assert games[0].bridged >= 1


def test_two_games_in_one_stream():
    readings = stream_for(ITALIAN) + stream_for(SICILIAN)
    games = run(readings)
    assert len(games) == 2
    assert games[0].moves_san == ITALIAN
    assert games[1].moves_san == SICILIAN


def test_replay_of_an_earlier_game_is_dropped():
    # a game, then the narrator stepping back through it six plies a second
    # (as read at refine.STALL_FPS) - not a second game
    line = ITALIAN + ["Bd2", "Bxd2+", "Nbxd2", "d5"]
    game = stream_for(line)
    replay = [(game[-1][0] + 30 + ts / 6, p) for ts, p in stream_for(line, hold=1)]
    games = run(game + replay)
    assert [g.moves_san for g in games] == [line]


def test_same_opening_played_again_is_kept():
    # the same line in the next game, at playing speed, is a real game
    line = ITALIAN + ["Bd2", "Bxd2+", "Nbxd2", "d5"]
    game = stream_for(line)
    again = [(game[-1][0] + 30 + ts, p) for ts, p in stream_for(line)]
    games = run(game + again)
    assert [g.moves_san for g in games] == [line, line]


def test_slow_replay_with_frozen_clocks_is_dropped():
    # stepped through at playing speed, a review is only told apart from a
    # real game by its clocks: read, but never running
    line = ITALIAN + ["Bd2", "Bxd2+", "Nbxd2", "d5"]
    game = stream_for(line)
    games = run(game + [(game[-1][0] + 30 + ts, p) for ts, p in stream_for(line)])
    first, again = games
    first.clock_white_seconds = [600 - i for i in range(len(line))]
    again.clock_black_seconds = [189] * len(line)
    assert not reconstruct.is_frozen_replay(first, [])
    assert reconstruct.is_frozen_replay(again, [first])
    again.clock_black_seconds = [189 - i for i in range(len(line))]
    assert not reconstruct.is_frozen_replay(again, [first])


def test_long_gap_ends_game():
    readings = stream_for(ITALIAN[:6])
    readings += [(readings[-1][0] + i, None) for i in range(1, reconstruct.GAP_FRAMES + 2)]
    readings += stream_for(SICILIAN)
    games = run(readings)
    assert [g.moves_san for g in games] == [ITALIAN[:6], SICILIAN]


def test_short_fragment_is_dropped():
    games = run(stream_for(["e4"]))
    assert games == []


RUY_LOPEZ_EXCHANGE = ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Bxc6", "dxc6", "O-O"]


def test_has_wasted_round_trip_detects_pieces_returning_home():
    # both knights sortie out and all the way back - the position afterwards
    # is identical to the start, achieved via 4 entirely wasted moves
    board = chess.Board()
    path = [chess.Move.from_uci(u) for u in ("g1f3", "g8f6", "f3g1", "f6g8")]
    assert reconstruct._has_wasted_round_trip(board, path)


def test_has_wasted_round_trip_ignores_genuine_progress():
    board = chess.Board()
    path = [chess.Move.from_uci(u) for u in ("e2e4", "e7e5", "g1f3", "b8c6")]
    assert not reconstruct._has_wasted_round_trip(board, path)


def test_has_wasted_round_trip_ignores_a_developing_capture():
    # a bishop develops through a square that starts (and later, briefly,
    # ends up) empty on its way to capturing a piece - an ordinary bridge,
    # not a wasted round trip, even though b5 "reverts" to empty
    board = chess.Board()
    for san in ("e4", "e5", "Nf3", "Nc6"):
        board.push_san(san)
    path = []
    b = board.copy()
    for san in ("Bb5", "a6", "Bxc6", "dxc6", "O-O"):
        mv = b.parse_san(san)
        path.append(mv)
        b.push(mv)
    assert not reconstruct._has_wasted_round_trip(board, path)


def test_has_wasted_round_trip_ignores_a_capture_and_recapture():
    # exd4 cxd4: a genuinely different pawn ends up on d4 than the one that
    # started there (the original was captured and is gone) - the square's
    # occupant *type* reverts (pawn -> pawn) but no specific piece actually
    # round-tripped. A real bug: this was flagged by an earlier, cruder
    # version of the check that only compared resulting type/color per
    # square, dropping a real 82-ply game's confidence from 0.87 to 0.54 on
    # an ordinary isolated-pawn trade.
    board = chess.Board()
    for san in ("c4", "e6", "g3", "d5", "Bg2", "Nf6", "Nf3", "d4", "O-O", "c5", "e3", "Nc6"):
        board.push_san(san)
    path = []
    b = board.copy()
    for san in ("exd4", "cxd4", "Na3"):
        mv = b.parse_san(san)
        path.append(mv)
        b.push(mv)
    assert not reconstruct._has_wasted_round_trip(board, path)


def test_has_wasted_round_trip_ignores_ordinary_castling():
    # castling moves both the king and the rook - neither should look like
    # it "returned" anywhere just from the act of castling itself
    board = chess.Board()
    for san in ("e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"):
        board.push_san(san)
    path = []
    b = board.copy()
    for san in ("O-O", "Nf6", "d3", "d6"):
        mv = b.parse_san(san)
        path.append(mv)
        b.push(mv)
    assert not reconstruct._has_wasted_round_trip(board, path)


def test_a_single_misread_frame_does_not_trigger_a_spurious_bridge():
    # Real bug, found on a real video: one frame misread a pawn capture
    # (fxg5) as the captured square simply going empty - no piece landing on
    # it at all, which is impossible via any real move. _find_sequence used
    # to "explain" that impossible reading with a legal-but-nonsensical
    # 5-ply bridge (a bishop and a rook each taking an excursion and
    # returning to their own starting squares) purely because it reached an
    # exact pixel match - silently corrupting the game from that point on.
    # It must now prefer the much simpler, correct single-move explanation
    # instead (or give up on this one reading) rather than accept a
    # round-trip-laden "exact" bridge.
    setup = ["e4", "c6", "Nf3", "d5", "Nc3", "dxe4", "Nxe4", "Nf6", "Neg5", "h6",
             "Nxf7", "Kxf7", "Bc4+", "Ke8", "Ne5", "e6", "Qe2", "Qd6", "d3", "g5",
             "f4", "b5", "Bb3", "Rg8"]
    board = chess.Board()
    for san in setup:
        board.push_san(san)
    before = _placement_of(board)

    after_fxg5 = board.copy()
    after_fxg5.push_san("fxg5")
    correct = _placement_of(after_fxg5)

    # corrupt: the captured square (g5) misread as empty instead of holding
    # the capturing white pawn - impossible via any real move, but exactly
    # what a bad frame produced on the real video
    g5_index = (7 - chess.square_rank(chess.G5)) * 8 + chess.square_file(chess.G5)
    corrupted = correct[:g5_index] + "." + correct[g5_index + 1:]

    readings = stream_for(setup, hold=1)
    readings.append((readings[-1][0] + 1.0, corrupted))
    games = run(readings)

    assert len(games) == 1
    # must not be the spurious round-trip bridge
    assert games[0].moves_san[:24] != setup[:23] + ["Rxg5"]
    for san in games[0].moves_san:
        assert san != "Rxg5"


def test_clock_evidence_rejects_a_cancelled_premove():
    # Real bug, found on a real video: a cancelled/changed premove put a
    # different (but fully legal) move on the board for several sampled
    # frames before the player's *actual* move appeared there instead -
    # board content alone can't tell a genuine move from an abandoned
    # premove preview, but the mover's clock not moving at all across
    # several real seconds can. Modeled here as a bridge where white's c3
    # is real either way but black's response differs: the first, fake
    # placement (Qf6) is credited to black without their clock ever
    # ticking; the second, real one (Nf6) comes with an actual clock drop.
    opening = ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"]
    board = chess.Board()
    for san in opening:
        board.push_san(san)

    fake_board = board.copy()
    fake_board.push_san("c3")
    fake_board.push_san("Qf6")
    fake_placement = _placement_of(fake_board)

    real_board = board.copy()
    real_board.push_san("c3")
    real_board.push_san("Nf6")
    real_placement = _placement_of(real_board)

    readings = stream_for(opening, hold=1)
    last_ts = readings[-1][0]
    readings += [
        (last_ts + 3.0, fake_placement),
        (last_ts + 4.0, fake_placement),
        (last_ts + 5.0, fake_placement),
        (last_ts + 8.0, real_placement),
        (last_ts + 9.0, real_placement),
    ]
    clock_by_side = {
        "black": [
            (last_ts, 300.0),
            (last_ts + 5.0, 300.0),   # frozen through the whole fake window
            (last_ts + 8.0, 295.0),   # ticked by the time the real move appears
        ],
    }

    without_clock = run(readings)
    with_clock = run(readings, clock_by_side=clock_by_side)

    # The artifact used to win without clock evidence; the lookahead now
    # drops it on its own (the real placement follows from the position
    # before it, not from after it), clocks or no clocks.
    assert without_clock[0].moves_san[-2:] == ["c3", "Nf6"]
    assert with_clock[0].moves_san[-2:] == ["c3", "Nf6"]


def test_multi_ply_gap_is_bridged():
    # drop 4 consecutive plies (5-8, Bb5 through dxc6) with a further move (O-O)
    # confirming the position afterwards: a wider gap than a single skipped
    # frame, e.g. a stretch the camera/board was briefly unreadable. The search
    # must explore past the old fixed 3-ply move budget to bridge it in one hop.
    games = run(stream_for(RUY_LOPEZ_EXCHANGE, drop=(5, 6, 7, 8)))
    assert len(games) == 1
    assert games[0].moves_san == RUY_LOPEZ_EXCHANGE
    assert games[0].bridged >= 1


def _positions(sans):
    board = chess.Board()
    out = [_placement_of(board)]
    for san in sans:
        board.push_san(san)
        out.append(_placement_of(board))
    return out


def test_browsing_back_through_the_move_list_adds_no_moves():
    # Real pattern (several videos): mid-game the viewer steps back two plies
    # with the arrow keys, then forward again, then the game goes on. Each
    # backward step looks like a legal move ("Bc5" undone reads as "Bf8"),
    # which used to derail the game from there.
    played = ITALIAN[:8]
    pos = _positions(ITALIAN)
    readings = stream_for(played)
    ts = readings[-1][0] + 1
    for p in (pos[7], pos[6], pos[7], pos[8]):     # back, back, forward, live again
        readings += [(ts, p), (ts + 1, p)]
        ts += 2
    for p in pos[9:]:                               # the game continues
        readings += [(ts, p), (ts + 1, p)]
        ts += 2
    games = run(readings)
    assert games[0].moves_san == ITALIAN


def test_a_real_repetition_is_still_played():
    # Knights out and back is a genuine repetition, not browsing: the
    # position after ply 4 is reached from ply 1 by 3 real plies (odd).
    shuffle = ["e4", "Nf6", "Nf3", "Ng8", "Ng1", "Nf6", "Nf3", "e6"]
    games = run(stream_for(shuffle))
    assert games[0].moves_san == shuffle


def test_a_shown_and_undone_line_is_retracted():
    # Real pattern (recap video): a line that was NOT played is shown, then
    # stepped back - and each backward step is itself a legal move (Bf5 Nd4
    # Bc8 N4f3), so the game used to gain the whole loop. The undo steps
    # arrive bunched in one reading, unlike a real repetition's moves.
    played = ITALIAN[:8]
    board = chess.Board()
    for san in played:
        board.push_san(san)
    loop = []
    for san in ("Ng5", "Qe7", "Nf3", "Qd8"):   # out and back; the game went d4
        board.push_san(san)
        loop.append(_placement_of(board))
    pos = _positions(ITALIAN)
    readings = stream_for(played)
    ts = readings[-1][0] + 1
    readings += [(ts, loop[0]), (ts + 1, loop[1])]         # shown one ply at a time...
    readings += [(ts + 2, loop[2]), (ts + 2, loop[3])]     # ...undone in one burst
    ts += 3
    for p in pos[9:]:
        readings += [(ts, p), (ts + 1, p)]
        ts += 2
    assert run(readings)[0].moves_san == ITALIAN


def _hold(readings, placement, n, ts):
    for _ in range(n):
        readings.append((ts, placement))
        ts += 1.0
    return ts


def test_a_premove_display_is_waited_out():
    # After 5.dxc5 (Black to move) White premoves Rxd1: the board draws the
    # rook on d1 (over the queen) and a1 empty, until Black plays 5...Qa5.
    sans = ["d4", "d5", "Bf4", "c5", "Nc3", "Nf6", "e4", "dxe4", "dxc5"]
    readings = stream_for(sans, hold=3)
    ts = readings[-1][0] + 1.0
    board = chess.Board()
    for san in sans:
        board.push_san(san)
    shown = list(_placement_of(board))
    a1, d1 = reconstruct._placement_of(board).index("R"), 7 * 8 + 3
    shown[a1], shown[d1] = ".", "R"
    ts = _hold(readings, "".join(shown), 20, ts)       # premove shown for 20 s
    board.push_san("Qa5")
    ts = _hold(readings, _placement_of(board), 3, ts)
    board.push_san("Bb5+")
    _hold(readings, _placement_of(board), 3, ts)
    games = run(readings)
    assert len(games) == 1
    assert games[0].moves_san == sans + ["Qa5", "Bb5+"]


def test_a_move_taken_back_is_replaced_by_the_move_played():
    # 38.c4 appears, the board reverts, then 38.Bg6 is played instead.
    sans = ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"]
    readings = stream_for(sans, hold=3)
    ts = readings[-1][0] + 1.0
    board = chess.Board()
    for san in sans:
        board.push_san(san)
    before = _placement_of(board)
    board.push_san("c3")
    ts = _hold(readings, _placement_of(board), 3, ts)   # c3 shown
    board.pop()
    ts = _hold(readings, before, 3, ts)                  # taken back
    board.push_san("d3")
    ts = _hold(readings, _placement_of(board), 3, ts)   # d3 played instead
    board.push_san("d6")
    _hold(readings, _placement_of(board), 3, ts)
    games = run(readings)
    assert games[0].moves_san == sans + ["d3", "d6"]


def test_browsing_back_one_ply_and_returning_keeps_the_move():
    sans = ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"]
    readings = stream_for(sans, hold=3)
    ts = readings[-1][0] + 1.0
    board = chess.Board()
    for san in sans:
        board.push_san(san)
    after = _placement_of(board)
    board.pop()
    ts = _hold(readings, _placement_of(board), 3, ts)   # viewer steps back one ply
    board.push_san("Bc5")
    ts = _hold(readings, after, 3, ts)                   # and forward again
    board.push_san("c3")
    _hold(readings, _placement_of(board), 3, ts)
    games = run(readings)
    assert games[0].moves_san == sans + ["c3"]


def test_a_mid_move_frame_does_not_become_a_spurious_bridge():
    # The frame catching Rxb2 half-drawn (rook on both squares) is matched
    # exactly by a 2-ply story; the next frame shows the real position.
    sans = ["e4", "e5", "Nf3", "Nc6", "Bc4", "Nf6", "Ng5", "d5", "exd5", "Nxd5", "Nxf7", "Kxf7", "Qf3+", "Ke6", "Nc3", "Nb4"]
    readings = stream_for(sans, hold=3)
    ts = readings[-1][0] + 1.0
    board = chess.Board()
    for san in sans:
        board.push_san(san)
    board.push_san("a3")
    after = _placement_of(board)
    # mid-move frame: pawn drawn on a3 while a2 still shows it
    mid = list(after)
    mid[8 * (8 - 2) + 0] = "P"   # a2 still occupied
    ts = _hold(readings, "".join(mid), 1, ts)
    ts = _hold(readings, after, 3, ts)
    board.push_san("Nxc2+")
    _hold(readings, _placement_of(board), 3, ts)
    games = run(readings)
    assert games[0].moves_san[:len(sans) + 1] == sans + ["a3"]
    assert games[0].moves_san == sans + ["a3", "Nxc2+"]


def test_a_lifted_king_does_not_block_the_moves_played_meanwhile():
    # White drags the king (a premove) while Black plays: frames show no
    # white king at all, yet the game goes on.
    sans = ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5", "d3", "d6"]
    readings = stream_for(sans, hold=3)
    ts = readings[-1][0] + 1.0
    board = chess.Board()
    for san in sans:
        board.push_san(san)
    board.push_san("Nc3")
    lifted = list(_placement_of(board))
    board.push_san("Nf6")
    lifted2 = list(_placement_of(board))
    for pl in (lifted, lifted2):
        pl[8 * 7 + 4] = "."          # e1 empty: the king is in hand
    ts = _hold(readings, "".join(lifted), 2, ts)
    ts = _hold(readings, "".join(lifted2), 4, ts)
    board.push_san("Ke2")            # the drag ends
    ts = _hold(readings, _placement_of(board), 3, ts)
    board.push_san("O-O")
    _hold(readings, _placement_of(board), 3, ts)
    games = run(readings)
    assert games[0].moves_san == sans + ["Nc3", "Nf6", "Ke2", "O-O"]


def test_skipped_readings_pick_the_played_route_through_a_transposition():
    # Bh6 Qf6 Bg5 and Bg5 Qf6 reach the same position; a noisy frame of the
    # position after Bh6 Qf6 was skipped on the way and must decide it.
    sans = ["e4", "e5", "Nf3", "Nc6", "Bc4", "Nf6", "d3", "Be7", "Nc3", "d6", "Bg5", "h6", "Bh4", "O-O", "O-O", "Be6"]
    readings = stream_for(sans, hold=3)
    ts = readings[-1][0] + 1.0
    board = chess.Board()
    for san in sans:
        board.push_san(san)
    board.push_san("Bg3")            # bishop h4-g3 ...
    board.push_san("Qd7")
    mid = list(_placement_of(board))
    mid[8 * 6 + 0] = "."             # a2 misread: a square off, so this frame is skipped
    ts = _hold(readings, "".join(mid), 2, ts)
    board.push_san("Bh4")            # ... and back: same position as Bh4 staying put + Qd7
    ts = _hold(readings, _placement_of(board), 3, ts)
    board.push_san("Rfd8")
    _hold(readings, _placement_of(board), 3, ts)
    games = run(readings)
    assert games[0].moves_san == sans + ["Bg3", "Qd7", "Bh4", "Rfd8"]


def test_covered_square_is_one_of_our_pieces_read_empty_away_from_the_move():
    board = chess.Board()
    board.push_san("e4")
    here = _placement_of(board)
    move_squares = {chess.E2 ^ 56, chess.E4 ^ 56}
    queen_gone = here[:chess.D1 ^ 56] + "." + here[(chess.D1 ^ 56) + 1:]
    assert reconstruct._covered(here, queen_gone, move_squares)
    pawn_gone = here[:chess.E4 ^ 56] + "." + here[(chess.E4 ^ 56) + 1:]
    assert not reconstruct._covered(here, pawn_gone, move_squares)       # the move's own square
    extra = here[:chess.E5 ^ 56] + "p" + here[(chess.E5 ^ 56) + 1:]
    assert not reconstruct._covered(here, extra, move_squares)           # a piece where we have none


def test_two_pieces_read_with_their_colours_swapped_are_found():
    from reconstruct import _colour_swap
    cur = "Q" + "." * 62 + "q"
    assert _colour_swap(cur, "q" + "." * 62 + "Q") == {0, 63}
    # one flip alone is a capture's footprint, not a misread
    assert _colour_swap(cur, "q" + "." * 63) == set()
    # two pieces of different kinds aren't swapped
    assert _colour_swap("Q" + "." * 62 + "r", "q" + "." * 62 + "R") == set()
