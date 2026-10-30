import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import trust  # noqa: E402

MOVES = ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5", "c3", "Nf6", "d4", "exd4", "cxd4", "Bb4+"]


def test_moves_seen_one_at_a_time_are_trusted_but_the_last_two():
    conf, trusted = trust.assess(MOVES, list(range(len(MOVES))))
    assert trusted == len(MOVES) - trust.TAIL_PLIES
    assert conf[0] == trust.CONF_SINGLE and conf[-1] == trust.CONF_TAIL


def test_trust_ends_before_a_big_bunch():
    times = [1, 2, 3, 4, 5, 6, 7, 7, 7, 10, 11, 12]     # plies 7-9 seen at once
    conf, trusted = trust.assess(MOVES, times)
    assert trusted == 6 - trust.TRIGGER_MARGIN
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


def test_a_fuzzy_bunch_ends_trust_before_it():
    import trust
    sans = ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5", "c3", "Nf6", "d4", "exd4", "cxd4", "Bb4+"]
    secs = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 9.0, 11.0, 12.0]
    conf, trusted = trust.assess(sans, secs, fuzzy=[8, 9])
    assert trusted == 8 - trust.TRIGGER_MARGIN
    assert conf[8] <= trust.CONF_FUZZY and conf[9] <= trust.CONF_FUZZY


def test_trust_ending_just_after_a_rough_ply_ends_at_it():
    times = list(range(len(MOVES)))                     # trusted up to the last two: 10
    assert trust.assess(MOVES, times, rough=[7])[1] == 7
    assert trust.assess(MOVES, times, rough=[3])[1] == 10     # too far back to matter
    assert trust.assess(MOVES, times, rough=[10])[1] == 10    # past where trust ends anyway


def test_a_doubtful_ply_followed_by_clean_ones_does_not_end_trust():
    moves = ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4", "Nf6", "O-O", "Be7", "Re1", "b5", "Bb3", "d6",
             "c3", "O-O", "h3", "Nb8", "d4", "Nbd7", "c4", "c6"]
    times = list(range(len(moves)))
    # doubtful at ply 5 with eight clean plies after it: trust runs on to the usual holdback
    _, trusted = trust.assess(moves, times, doubtful=[5])
    assert trusted == len(moves) - trust.TAIL_PLIES
    # ...but not when one of those plies is marked too
    _, trusted = trust.assess(moves, times, doubtful=[5], rough=[9])
    assert trusted == 5 - trust.TRIGGER_MARGIN
