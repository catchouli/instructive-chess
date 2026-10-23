"""Keep the catalog and accuracy table in README.md up to date.

Rewrites only the block between the README_STATS markers, and only when the
numbers have moved enough to be worth a commit: the catalog grew or shrank by
5% or more, a percentage moved by a point or more, or the count of wrong shown
moves changed. Run it after merging a batch.

    scanner/.venv/bin/python scanner/tests/readme_stats.py                    # runs the benchmark
    scanner/.venv/bin/python scanner/tests/readme_stats.py --bench bm.json    # reuses a saved benchmark.py --json
    ... --force                                                               # write even if little changed
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
BLOCK = re.compile(r"<!-- README_STATS (\{.*?\}) -->\n.*?<!-- /README_STATS -->", re.S)
MIN_SHOWN_PLY = 12          # web/lib.js: games with fewer trusted plies aren't listed
GROWTH = 0.05               # catalog size change worth an update
POINTS = 1.0                # percentage-point change worth an update


def stats(games: list[dict], bench: dict) -> dict:
    m = [r for r in bench["rows"] if r["matched"]]
    shown = sum(r["trusted_ply"] for r in m)

    def pct(n, d):
        return round(100 * n / d, 1) if d else 0.0

    def trusted(g):
        return g["trusted_ply"] if isinstance(g.get("trusted_ply"), int) else len(g["moves_san"])

    return {
        "games": len(games),
        "videos": len({g["video_id"] for g in games}),
        "listed": sum(trusted(g) >= MIN_SHOWN_PLY for g in games),
        "checked": len(m),
        "checked_pct": pct(len(m), len(games)),
        "moves": pct(sum(r["moves_ok"] for r in m), len(m)),
        "clocks": pct(sum(r["clock"]["ok"] for r in m), len(m)),
        "ratings": pct(sum(r["rating_ok"] for r in m), len(m)),
        "shown_moves": shown,
        "shown_wrong": sum(max(0, r["trusted_ply"] - r["moves_correct"]) for r in m),
        "date": date.today().isoformat(),
    }


def significant(old: dict, new: dict) -> list[str]:
    """The changes worth an update, as readable lines (empty if none)."""
    why = []
    if not old:
        return ["no previous numbers"]
    if abs(new["games"] - old["games"]) >= GROWTH * old["games"]:
        why.append(f"games {old['games']} -> {new['games']}")
    for k in ("checked_pct", "moves", "clocks", "ratings"):
        if abs(new[k] - old[k]) >= POINTS:
            why.append(f"{k} {old[k]}% -> {new[k]}%")
    if new["shown_wrong"] != old["shown_wrong"]:
        why.append(f"wrong shown moves {old['shown_wrong']} -> {new['shown_wrong']}")
    return why


def render(s: dict) -> str:
    return f"""<!-- README_STATS {json.dumps(s)} -->
**{s['games']:,} games** from **{s['videos']:,} videos**, of which **{s['listed']:,}** are listed on
the site. {s['checked']:,} of them ({s['checked_pct']}%) have been found in the chess.com or lichess
archives and checked against the real game:

| Of the games checked against the real game | |
|---|---|
| Every move right | {s['moves']}% |
| Clocks: at least {benchmark.CLOCK_GAME_OK_FRAC:.0%} of readings within {benchmark.CLOCK_TOLERANCE_S} s | {s['clocks']}% |
| Ratings: both players within {benchmark.RATING_TOLERANCE} points | {s['ratings']}% |
| Wrong moves shown on the site | {s['shown_wrong']} of {s['shown_moves']:,} |

These figures cover only the games that could be found in the archives. The rest often
couldn't be matched because the scanner read too little of the game to tell which one it
was, so accuracy over the whole catalog is probably lower. The site shows only the moves the
scanner is sure of, so a game with a misread move is cut short rather than shown wrong.

*Last updated {s['date']} by `scanner/tests/readme_stats.py`.*
<!-- /README_STATS -->"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalog", type=Path, default=benchmark.CATALOG)
    ap.add_argument("--bench", type=Path, help="a saved `benchmark.py --json` of the same catalog")
    ap.add_argument("--force", action="store_true", help="write even if the numbers barely moved")
    args = ap.parse_args(argv)

    games = json.loads(args.catalog.read_text())
    bench = json.loads(args.bench.read_text()) if args.bench else benchmark.run(args.catalog)
    if bench["n_catalog"] != len(games):
        sys.exit(f"benchmark covers {bench['n_catalog']} games, the catalog has {len(games)}: rerun it")

    text = README.read_text()
    found = BLOCK.search(text)
    if not found:
        sys.exit("README.md has no README_STATS block")
    old, new = json.loads(found.group(1)), stats(games, bench)
    why = significant(old, new)
    if not why and not args.force:
        print("no significant change; README left alone")
        return 0
    README.write_text(text[:found.start()] + render(new) + text[found.end():])
    print("README updated:", "; ".join(why) or "forced")
    return 0


if __name__ == "__main__":
    sys.exit(main())
