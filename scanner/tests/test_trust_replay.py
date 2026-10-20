"""trust_replay.py: which plies a rule marks doubtful, from the bunch log."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import trust_replay  # noqa: E402

GAME = {"moves_san": ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6"],
        # bunches: plies 0-1, 2 (after an undo), 3-5 (after a reading a square off)
        "bunches": [[0, []], [2, ["undo"]], [3, ["unreach1"]]]}


def test_no_rules_no_doubt():
    assert trust_replay.doubtful(GAME, []) == []


def test_retract_marks_the_bunch_after_it():
    assert trust_replay.doubtful(GAME, [trust_replay.RULES["retract"]]) == [2]


def test_unreach_bunch_needs_two_plies():
    assert trust_replay.doubtful(GAME, [trust_replay.RULES["unreach_bunch"]]) == [3, 4, 5]
    single = {**GAME, "bunches": [[0, []], [5, ["unreach1"]]]}
    assert trust_replay.doubtful(single, [trust_replay.RULES["unreach_bunch"]]) == []
    assert trust_replay.doubtful(single, [trust_replay.RULES["unreach_any"]]) == [5]


def test_rules_combine():
    rules = [trust_replay.RULES["retract"], trust_replay.RULES["unreach_bunch"]]
    assert trust_replay.doubtful(GAME, rules) == [2, 3, 4, 5]
