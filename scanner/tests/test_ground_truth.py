"""Regression suite: run reconstruction against a cached per-frame reading stream
for a real video and check the games it produces against verified ground truth
(a fixture pulled from the player's real chess.com game archive).

This only re-runs recognize.vote_stream + reconstruct.reconstruct, using a
<video_id>-readings.json cache of raw per-frame board readings (see
scanner/tests/dump_readings.py) rather than the source video, so it's fast enough
to run on every change to reconstruct.py or vote_stream. It's skipped
automatically if the cache isn't present locally (the source videos and their
readings caches aren't checked into git - regenerate with dump_readings.py).

To add a fixture for a new video:
  1. scanner/.venv/bin/python scanner/tests/dump_readings.py <video-id-or-path>
  2. Find the player's real chess.com games for that day (ground_truth.fetch_chesscom_month)
     and save the cleaned list as scanner/tests/fixtures/<name>.json
  3. Add a case to CASES below.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ground_truth as gt   # noqa: E402
import recognize            # noqa: E402
import reconstruct           # noqa: E402

CACHE = Path(__file__).resolve().parents[1] / "cache"

# video_id -> (fixture name, minimum acceptable move_accuracy, minimum clean games)
CASES = {
    # Accuracy here dropped from 100% to ~68% after fixing a real-blob-frac
    # false-positive bug (a small noise blob, frac~0.057, scoring a confident
    # wrong piece match - see recognize._MIN_PIECE_FRAC) that was previously
    # truncating every reconstructed game down to tiny (3-5 ply), always-100%-
    # -correct fragments. Net effect on this video: 6 games now averaging ~14
    # plies instead of ~3.7, i.e. real recall went up a lot - but one game's
    # search still constructs a coherent, legal, but wrong alternate
    # continuation partway through (moves happening faster than 1fps can
    # track, similar to a known limitation on other videos - see project
    # memory). Tried and reverted a tolerance loosening aimed at this same
    # class of problem elsewhere (see reconstruct.FUZZ) because it caused
    # *worse* regressions than it fixed; a real fix likely needs either a
    # smarter search or scanning fast-paced videos at a higher --fps. 65%
    # threshold reflects the current, still much-improved, real baseline -
    # don't raise it back toward 97% without a real fix for the wrong-tail
    # case, and don't lower it further without re-investigating first.
    "KfuyDm5uksI": ("imrosen_2023-10", 0.65, 3),
    # "I Unknowingly Faced the ALIEN GAMBIT" - real game is on lichess (linked in the
    # video description), not chess.com; fixture built from lichess's single-game
    # export. Board orientation was flipped (Rosen played Black) - this case is what
    # caught the orientation bug (recognize.calibrate_from_start) in the first place:
    # before that fix this reconstructed a fully legal but entirely unrelated game
    # (0% accuracy). 60% threshold leaves headroom above the current ~68% while still
    # catching a real regression back toward that failure mode.
    "9h8UcYJZz-Q": ("rosen_alien_gambit_lichess", 0.60, 0),
}


def _readings_for(video_id: str):
    path = CACHE / f"{video_id}-readings.json"
    if not path.exists():
        pytest.skip(f"no cached readings for {video_id} - run dump_readings.py first")
    return json.loads(path.read_text())["readings"]


@pytest.mark.parametrize("video_id", list(CASES))
def test_accuracy_against_real_games(video_id):
    fixture_name, min_accuracy, min_clean = CASES[video_id]
    readings = _readings_for(video_id)
    real_games = gt.load_fixture(fixture_name)

    voted = recognize.vote_stream(readings)
    games = reconstruct.reconstruct(voted, raw=dict(readings))
    rep = gt.report([g.moves_san for g in games], real_games)

    print(f"\n{video_id}: {rep['n_games']} reconstructed games, "
          f"{rep['n_clean']} exact-prefix matches, "
          f"move accuracy {rep['move_accuracy']:.1%}")
    for g, m in zip(games, rep["matches"]):
        who = f"{m['game']['white']} vs {m['game']['black']}" if m["game"] else "no match"
        print(f"  t={int(g.start_seconds)}s ply={g.ply} conf={g.confidence:.2f} "
              f"-> {who}  correct {m['correct_prefix']}/{m['recon_len']} "
              f"(real game has {m['real_len']} plies) clean={m['clean']}")

    assert rep["move_accuracy"] >= min_accuracy, (
        f"reconstruction move accuracy {rep['move_accuracy']:.1%} dropped below "
        f"baseline {min_accuracy:.0%} for {video_id}")
    assert rep["n_clean"] >= min_clean, (
        f"only {rep['n_clean']} game(s) matched a real game's moves exactly, "
        f"expected at least {min_clean}")
