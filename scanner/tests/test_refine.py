import math
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


def test_a_partial_split_is_no_split():
    # Real case: a 4-ply bunch split 1+1+2 left a trusted pair holding the
    # wrong move - a bunch is split fully or not at all.
    path = _path("Nf3", "Nc6", "Bc4")
    dense = [(10.5, path[1]), (10.7, path[1]), (11.0, path[2]), (11.2, path[2])]
    assert refine.split_bunch(path, dense, 12.0) is None


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


class _Frames:
    """A stand-in DenseFrames: every grid frame in [a, b] reads as `name`."""
    def __init__(self, name):
        self.name, self.added = name, 0

    def read(self, a, b):
        # the grid frames in [a, b], as DenseFrames.read picks them
        return [(k / refine.DENSE_FPS, self.name) for k in range(math.ceil(a * refine.DENSE_FPS - 1e-6),
                                                                 math.floor(b * refine.DENSE_FPS + 1e-6) + 1)]

    def covers(self, a, b):
        return True


def test_segmented_frames_split_a_window_at_the_board_move():
    fr = refine.SegmentedFrames([(0.0, _Frames("old box")), (10.0, _Frames("new box"))], readings=[])
    got = fr.read(9.5, 10.5)
    assert {p for t, p in got if t < 10} == {"old box"} and {p for t, p in got if t >= 10} == {"new box"}
    assert [t for t, _ in got] == sorted(t for t, _ in got)


def test_segmented_frames_without_templates_give_the_readings_back():
    fr = refine.SegmentedFrames([(0.0, _Frames("old box")), (10.0, None)], readings=[(10.0, "a"), (11.0, None), (12.0, "b")])
    assert fr.read(10.0, 11.5) == [(10.0, "a"), (11.0, None)]
