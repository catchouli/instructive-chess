"""catalog.py's derived fields: the narrator's side, the speedrun tag, and
the per-video rating reconciliation."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import catalog  # noqa: E402


def test_is_speedrun_from_title():
    assert catalog.is_speedrun("OH NO MY QUEEN | Tricks & Traps Speedrun Episode 13")
    assert catalog.is_speedrun("Beginner To Master | Chess Speedrun | 1400")
    assert catalog.is_speedrun("Climbing the Rating Ladder vs. 1410")
    assert not catalog.is_speedrun("My BEST Titled Tuesday")
    assert not catalog.is_speedrun("")


def test_tag_pov_and_override():
    e = {"flipped": True, "video_title": "My BEST Titled Tuesday"}
    catalog.tag(e)
    assert e["pov"] == "black" and e["speedrun"] is False
    e = {"flipped": False, "video_title": "Speedrun Episode 9"}
    catalog.tag(e)
    assert e["pov"] == "white" and e["speedrun"] is True
    catalog.tag(e, speedrun=False)
    assert e["speedrun"] is False


def _game(pov, mine, theirs):
    w, b = (mine, theirs) if pov == "white" else (theirs, mine)
    e = {"pov": pov}
    if w is not None:
        e["rating_white"] = w
    if b is not None:
        e["rating_black"] = b
    return e


def test_reconcile_blanks_narrator_outliers_and_weak_opponents_at_titled_level():
    games = [_game("white", 2565, 2388), _game("black", 2567, 2427), _game("white", 2572, None),
             _game("white", 172, None),       # 2629 read as 172: digits lost
             _game("white", 261, 261)]        # both sides junk
    assert catalog.reconcile_ratings(games) == 3
    assert "rating_white" not in games[3]
    assert "rating_white" not in games[4] and "rating_black" not in games[4]
    assert games[0] == {"pov": "white", "rating_white": 2565, "rating_black": 2388}


def test_reconcile_median_prefers_four_digit_reads_on_a_blurry_video():
    # BdcY0WCN29Y: most narrator reads are truncated junk; the two 4-digit
    # reads (one right) must define the median, or the right one is blanked.
    games = [_game("white", 192, 431), _game("black", 1273, 2933), _game("white", 242, 152),
             _game("white", 2747, 2741), _game("black", 441, 100), _game("white", None, 2563)]
    catalog.reconcile_ratings(games)
    assert games[3] == {"pov": "white", "rating_white": 2747, "rating_black": 2741}
    assert games[5] == {"pov": "white", "rating_black": 2563}
    assert "rating_white" not in games[0] and "rating_black" not in games[0]
    assert "rating_black" not in games[1]   # 1273 is far from 2747


def test_reconcile_leaves_a_speedrun_alone():
    games = [_game("white", 400, 405), _game("black", 400, 417), _game("white", 433, 420),
             _game("black", 450, 454), _game("white", 497, 504)]
    assert catalog.reconcile_ratings(games) == 0


def test_reconcile_blanks_the_opponent_when_both_sides_read_the_same():
    # one rating read twice: the narrator's agrees with the rest of the video
    games = [_game("white", 2540, 1611), _game("black", 2545, 1700), _game("white", 2540, 2540)]
    assert catalog.reconcile_ratings(games) == 1
    assert games[2] == {"pov": "white", "rating_white": 2540}


def test_reconcile_needs_three_narrator_reads():
    games = [_game("white", 2500, 2400), _game("white", 100, 2400)]
    assert catalog.reconcile_ratings(games) == 0


def test_drop_final_bunch():
    moves = ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4", "Nf6", "O-O", "Be7", "Re1", "b5", "Bb3"]
    times = [float(i) for i in range(11)] + [20.0, 20.0]       # the last two arrived together
    e = {"moves_san": list(moves), "move_seconds": list(times), "ply": 13, "pgn": "",
         "clock_white_seconds": list(range(13)), "fuzzy_plies": [11], "ambiguous_plies": [3, 12]}
    assert catalog.drop_final_bunch(e) == 2
    assert e["moves_san"] == moves[:11] and e["ply"] == 11 and len(e["clock_white_seconds"]) == 11
    assert "fuzzy_plies" not in e and e["ambiguous_plies"] == [3]
    assert e["pgn"].startswith("1. e4 e5 2. Nf3") and e["pgn"].endswith("6. Re1")
    single = {"moves_san": moves[:12], "move_seconds": [float(i) for i in range(12)], "ply": 12, "pgn": ""}
    assert catalog.drop_final_bunch(single) == 0                # a final single move stays
