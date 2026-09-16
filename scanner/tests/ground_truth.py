"""Match reconstructed games against real chess.com game archives (fixtures/) so
reconstruction accuracy can be measured against verified ground truth and tracked
for regressions as the CV/reconstruction pipeline changes.

Fixtures are pre-fetched snapshots of a player's chess.com game history (see
fetch_chesscom_month below to regenerate one) - plain SAN move lists, no clock
annotations, so they're stable to diff and don't require network access to run
the test suite.
"""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> list[dict]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def fetch_chesscom_month(username: str, year: int, month: int) -> list[dict]:
    """Hit the public chess.com API and return cleaned games for one month,
    oldest first. Used to build/refresh a fixture file; not called by tests."""
    url = f"https://api.chess.com/pub/player/{username}/games/{year:04d}/{month:02d}"
    with urllib.request.urlopen(url, timeout=20) as resp:
        data = json.load(resp)

    games = []
    for g in data.get("games", []):
        if g.get("rules") != "chess":
            continue
        body = g["pgn"].split("]\n\n")[-1]
        body = re.sub(r"\{[^}]*\}", "", body)
        body = re.sub(r"\s*(1-0|0-1|1/2-1/2|\*)\s*$", "", body)
        moves = re.sub(r"\d+\.(\.\.)?", "", body).split()
        games.append({
            "end_time": g["end_time"],
            "white": g["white"]["username"],
            "black": g["black"]["username"],
            "white_rating": g["white"]["rating"],
            "black_rating": g["black"]["rating"],
            "time_class": g["time_class"],
            "moves_san": moves,
        })
    games.sort(key=lambda g: g["end_time"])
    return games


def _common_prefix_len(a: list[str], b: list[str]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def best_match(moves_san: list[str], real_games: list[dict]) -> dict:
    """Find the real game whose moves share the longest prefix with *moves_san*,
    and report how much of the reconstruction that prefix covers."""
    best_game = None
    best_len = -1
    for g in real_games:
        n = _common_prefix_len(moves_san, g["moves_san"])
        if n > best_len:
            best_len, best_game = n, g
    recon_len = len(moves_san)
    return {
        "game": best_game,
        "correct_prefix": max(best_len, 0),
        "recon_len": recon_len,
        "real_len": len(best_game["moves_san"]) if best_game else 0,
        # every reconstructed move so far was correct (no wrong move slipped in,
        # though the game may continue further in reality)
        "clean": best_game is not None and best_len == recon_len,
    }


def report(games_san: list[list[str]], real_games: list[dict]) -> dict:
    """Match each reconstructed game (list of SAN moves) against the fixture and
    summarize accuracy across all of them."""
    matches = [best_match(m, real_games) for m in games_san]
    total_moves = sum(m["recon_len"] for m in matches)
    correct_moves = sum(m["correct_prefix"] for m in matches)
    return {
        "matches": matches,
        "n_games": len(matches),
        "n_clean": sum(1 for m in matches if m["clean"]),
        "move_accuracy": (correct_moves / total_moves) if total_moves else 0.0,
    }
