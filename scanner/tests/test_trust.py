import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import trust  # noqa: E402

MOVES = ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5", "c3", "Nf6", "d4", "exd4", "cxd4", "Bb4+"]


def test_moves_seen_one_at_a_time_are_trusted_but_the_last_two():
    conf, trusted = trust.assess(MOVES, list(range(len(MOVES))))
    assert trusted == len(MOVES) - trust.TAIL_PLIES
    assert conf[0] == trust.CONF_SINGLE and conf[-1] == trust.CONF_TAIL


def test_trust_ends_one_ply_before_a_big_bunch():
    times = [1, 2, 3, 4, 5, 6, 7, 7, 7, 10, 11, 12]     # plies 7-9 seen at once
    conf, trusted = trust.assess(MOVES, times)
    assert trusted == 5
    assert conf[6:9] == [trust.CONF_BUNCH_BIG] * 3


def test_an_early_pair_is_fine_and_a_clock_checked_mate_keeps_its_ending():
    mate = ["f3", "e5", "g4", "Qh4#"]
    conf, trusted = trust.assess(mate, [1, 2, 2, 3], clocks_checked_end=True)
    assert trusted == 4


def test_an_unchecked_mate_is_held_back_like_any_ending():
    # Videos show mating lines after a resignation too.
    mate = ["f3", "e5", "g4", "Qh4#"]
    conf, trusted = trust.assess(mate, [1, 2, 2, 3])
    assert trusted == 4 - trust.TAIL_PLIES
