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


def test_multi_ply_gap_is_bridged():
    # drop 4 consecutive plies (5-8, Bb5 through dxc6) with a further move (O-O)
    # confirming the position afterwards: a wider gap than a single skipped
    # frame, e.g. a stretch the camera/board was briefly unreadable. The search
    # must explore past the old fixed 3-ply move budget to bridge it in one hop.
    games = run(stream_for(RUY_LOPEZ_EXCHANGE, drop=(5, 6, 7, 8)))
    assert len(games) == 1
    assert games[0].moves_san == RUY_LOPEZ_EXCHANGE
    assert games[0].bridged >= 1
