import sys
from pathlib import Path

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import refine  # noqa: E402
from reconstruct import _placement_of  # noqa: E402


def _path(*sans):
    b = chess.Board()
    for san in ("e4", "e5"):
        b.push_san(san)
    out = []
    for san in sans:
        b.push_san(san)
        out.append(_placement_of(b))
    return out


def _misread(p):
    return p[:40] + ("P" if p[40] == "." else ".") + p[41:]


def test_positions_seen_in_order_time_each_ply():
    path = _path("Nf3", "Nc6", "Bc4")
    dense = [(10.0, path[0]), (10.2, _misread(path[0])), (10.3, path[0]),
             (10.5, path[1]), (10.7, path[1]), (11.0, path[2]), (11.2, path[2])]
    assert refine.split_bunch(path, dense, 12.0) == [10.0, 10.5, 11.0]


def test_an_unseen_position_stays_with_the_next_seen_one():
    path = _path("Nf3", "Nc6", "Bc4")
    dense = [(10.5, path[1]), (10.7, path[1]), (11.0, path[2]), (11.2, path[2])]
    assert refine.split_bunch(path, dense, 12.0) == [10.5, 10.5, 11.0]


def test_nothing_seen_apart_is_no_split():
    path = _path("Nf3", "Nc6")
    dense = [(11.0, path[1]), (11.2, path[1])]
    assert refine.split_bunch(path, dense, 12.0) is None


def test_positions_out_of_order_do_not_split():
    path = _path("Nf3", "Nc6")
    dense = [(10.0, path[1]), (10.2, path[1]), (10.5, path[0]), (10.7, path[0])]
    assert refine.split_bunch(path, dense, 12.0) is None


def test_a_run_never_matching_exactly_does_not_confirm():
    # One square off throughout is also what a wrong piece type on the
    # right square looks like - it must not confirm the move.
    path = _path("Nf3", "Nc6")
    dense = [(10.0, _misread(path[0])), (10.2, _misread(path[0])), (11.0, path[1]), (11.2, path[1])]
    assert refine.split_bunch(path, dense, 12.0) is None
