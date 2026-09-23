import sys
from pathlib import Path

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import refine  # noqa: E402
from reconstruct import _placement_of  # noqa: E402


def _start():
    b = chess.Board()
    for san in ("e4", "e5"):
        b.push_san(san)
    return b


def _path(*sans):
    b = _start()
    out = []
    for san in sans:
        b.push_san(san)
        out.append(_placement_of(b))
    return out


BEFORE = _placement_of(_start())


def _set(p, square, piece):
    i = (7 - chess.square_rank(square)) * 8 + chess.square_file(square)
    return p[:i] + piece + p[i + 1:]


def test_positions_seen_in_order_time_each_ply():
    path = _path("Nf3", "Nc6", "Bc4")
    dense = [(10.0, path[0]), (10.2, path[0]), (10.5, path[1]), (10.7, path[1]),
             (11.0, path[2]), (11.2, path[2])]
    assert refine.split_bunch(BEFORE, path, dense, 12.0) == [10.0, 10.5, 11.0]


def test_a_misread_square_the_moves_dont_touch_is_ignored():
    # Real case: one persistent misread elsewhere on the board hid every split.
    path = _path("Nf3", "Nc6")
    noisy = [_set(p, chess.A2, ".") for p in path]
    dense = [(10.0, noisy[0]), (10.2, noisy[0]), (10.5, noisy[1]), (10.7, noisy[1])]
    assert refine.split_bunch(BEFORE, path, dense, 12.0) == [10.0, 10.5]


def test_a_wrong_piece_on_a_square_the_moves_touch_does_not_confirm():
    # One square off *on the moves' squares* is what a wrong piece looks like
    # (a real misread: Ra7 for Qa7) - it must not confirm the move.
    path = _path("Nf3", "Nc6")
    wrong = _set(path[0], chess.F3, "B")
    dense = [(10.0, wrong), (10.2, wrong), (10.5, path[1]), (10.7, path[1])]
    assert refine.split_bunch(BEFORE, path, dense, 12.0) is None


def test_a_partial_split_is_no_split():
    # Real case: a 4-ply bunch split 1+1+2 left a trusted pair holding the
    # wrong move - a bunch is split fully or not at all.
    path = _path("Nf3", "Nc6", "Bc4")
    dense = [(10.5, path[1]), (10.7, path[1]), (11.0, path[2]), (11.2, path[2])]
    assert refine.split_bunch(BEFORE, path, dense, 12.0) is None


def test_positions_out_of_order_do_not_split():
    path = _path("Nf3", "Nc6")
    dense = [(10.0, path[1]), (10.2, path[1]), (10.5, path[0]), (10.7, path[0])]
    assert refine.split_bunch(BEFORE, path, dense, 12.0) is None


def test_a_single_frame_is_not_enough():
    path = _path("Nf3", "Nc6")
    dense = [(10.0, path[0]), (10.5, path[1]), (10.7, path[1])]
    assert refine.split_bunch(BEFORE, path, dense, 12.0) is None
