"""Regression tests for board_detect's aggregation logic.

Both bugs here were found on real videos: a low-contrast board theme where
every correct detection scored below the old absolute threshold and a rare
high-scoring false positive elsewhere in the frame won by default; and a
board whose independently-fit horizontal/vertical grid pitch disagreed,
producing a non-square box (impossible for a real chessboard).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pytest

import board_detect  # noqa: E402
from board_detect import Box  # noqa: E402


def test_detect_prefers_consensus_over_lone_high_scorer(monkeypatch):
    # 9 frames agree closely on the real board at a modest score; 1 frame
    # finds something else entirely at a higher score (e.g. a UI panel with
    # sharper contrast than this video's low-contrast board theme).
    consensus = [Box(330, 10, 610, 610, score=0.15 + 0.01 * i) for i in range(9)]
    outlier = [Box(378, 70, 528, 522, score=0.9)]
    hits = iter(consensus + outlier)
    monkeypatch.setattr(board_detect, "detect_in_frame", lambda f: next(hits))

    result = board_detect.detect([object()] * 10, min_score=0.05)
    assert result is not None
    assert result.x == 330 and result.y == 10
    assert result.w == 610 and result.h == 610


def test_detect_rejects_all_low_score_frames():
    result = board_detect.detect([], min_score=0.05)
    assert result is None


def test_refine_grid_forces_square_board():
    """Build a synthetic frame with a clean 8x8 checkerboard, then corrupt a
    horizontal strip near the bottom (as board coordinates/text or a UI
    element might) so the vertical grid-line fit alone would undershoot -
    the horizontal fit should still win out and the final box must be square.
    """
    size = 320
    cell = size // 8
    board = np.zeros((size, size), np.uint8)
    for r in range(8):
        for c in range(8):
            if (r + c) % 2 == 0:
                board[r * cell:(r + 1) * cell, c * cell:(c + 1) * cell] = 220
            else:
                board[r * cell:(r + 1) * cell, c * cell:(c + 1) * cell] = 60

    canvas = np.full((size + 80, size + 80), 128, np.uint8)
    ox, oy = 40, 40
    canvas[oy:oy + size, ox:ox + size] = board
    # blur out the grid lines in the bottom eighth of the board only, so the
    # vertical axis's line fit is starved of signal there and would (if fit
    # independently) prefer a shorter pitch that avoids that region
    canvas[oy + size - cell:oy + size, ox:ox + size] = 128

    coarse = board_detect.Box(ox, oy, size, size, score=1.0)
    refined = board_detect._refine_grid(canvas.astype(np.float32), coarse)
    assert refined.w == refined.h, f"board must be square, got {refined.w}x{refined.h}"


@pytest.mark.parametrize("gx_pitch,gy_pitch", [(40.0, 36.0), (36.0, 40.0)])
def test_fit_lines_pitch_reuse_matches_requested_pitch(gx_pitch, gy_pitch):
    # directly exercise the "pitch=" reuse path _refine_grid relies on
    profile = np.zeros(400)
    idx = np.round(10 + np.arange(9) * gy_pitch).astype(int)
    profile[idx] = 1.0
    start, pitch, _ = board_detect._fit_lines(profile, 0, 30, 30.0, 45.0, pitch=gx_pitch)
    assert pitch == gx_pitch
