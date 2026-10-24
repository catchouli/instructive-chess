"""Try trust rules in seconds instead of hours. A rule that only decides
which plies are doubtful (see reconstruct.UNREACH_BUNCH / RETRACT_ENDS_TRUST)
doesn't change which moves are picked, so the games can be rebuilt once,
with what happened just before each accepted bunch, and every rule replayed
on that log.

    scanner/.venv/bin/python scanner/tests/trust_replay.py build LOG.json VIDEO_ID [...] [--jobs N]
    scanner/.venv/bin/python scanner/tests/trust_replay.py replay LOG.json [--clocks CATALOG] [--marks CATALOG] RULE[+RULE] [...]

`build` reconstructs each video from its cached readings (the doubtful rules
off) and scores each game against its real game. `replay` prints, per rule
set, the same summary as the A/B scripts: wrong trusted games, trusted
plies, shown (12+ trusted) and whole openings. `--clocks` takes clock
presence from a catalog (by video + start), since trust ends earlier
without both clocks; without it, no game has clocks. trust.py's back-off
to rough plies (reconstruct.Game.rough) is always applied; set
trust.ROUGH_BACKOFF = 0 to see the rules without it. `--marks` takes each
game's move times and ambiguous/fuzzy plies from a scanned catalog where its
moves match the log: the log is built without refine.py, whose split bunches
no longer end trust, so without it a rule's leaks are judged on bunches the
real pipeline doesn't have (three "fixed" leaks came back in the 2026-10-20
rebuild this way).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import benchmark    # noqa: E402
import recognize    # noqa: E402
import reconstruct  # noqa: E402
import trust        # noqa: E402

CACHE = Path(__file__).resolve().parents[1] / "cache"

KINDS = [
    ("unreach1", re.compile(r"skip \(unreachable; 1 squares off\)")),
    ("unreach2+", re.compile(r"skip \(unreachable; [2-9]\d* squares off\)")),
    ("fuzzyamb", re.compile(r"skip \(fuzzy-ambiguous")),
    ("undo", re.compile(r"^undo")),
    # finer kinds of undo: one move shown briefly and taken back (a piece
    # dragged to show an idea - Naroditsky's habit while explaining), a
    # bunch shown briefly, a piece passing through, a move replaced
    ("undo1", re.compile(r"^undo: ply \d+ \S+ shown briefly")),
    ("undo_bunch", re.compile(r"^undo: plies")),
    ("undo_pass", re.compile(r"^undo: .*passing through")),
    ("undo_replace", re.compile(r"^undo: ply \d+ \S+ taken back$")),
    ("recover", re.compile(r"^recover")),
    ("browse", re.compile(r"^browsing back")),
    ("detour", re.compile(r"^detour")),
    ("premove", re.compile(r"^premove shown")),
    ("kinglift", re.compile(r"^king (lifted|missing)")),
    ("lookahead", re.compile(r"^lookahead")),
]
PLY = re.compile(r"^ply (\d+): ")

# A rule: (what happened before the bunch, bunch size) -> its plies are doubtful.
RULES = {
    "retract": lambda ctx, n: bool(ctx & {"undo", "browse", "detour"}),
    "undo_detour": lambda ctx, n: bool(ctx & {"undo", "detour"}),          # reconstruct.RETRACT_ENDS_TRUST
    "recover": lambda ctx, n: "recover" in ctx,                             # reconstruct.RECOVER marks
    "undo_but_brief1_detour": lambda ctx, n: bool(ctx & {"undo_bunch", "undo_pass", "undo_replace", "detour"}),
    "browse_detour": lambda ctx, n: bool(ctx & {"browse", "detour"}),
    "detour": lambda ctx, n: "detour" in ctx,
    "unreach_bunch": lambda ctx, n: "unreach1" in ctx and n >= 2,
    "unreach_any": lambda ctx, n: "unreach1" in ctx,
}


def _build_one(vid: str, keep_unmatched: bool = False) -> list[dict]:
    try:
        info = json.loads((CACHE / f"{vid}.info.json").read_text())
        readings = json.loads((CACHE / f"{vid}-readings.json").read_text())["readings"]
    except FileNotFoundError:
        return []
    reconstruct.UNREACH_BUNCH = reconstruct.RETRACT_ENDS_TRUST = 0.0
    notes: list[tuple[float, str]] = []
    games = [g for g in reconstruct.reconstruct(recognize.vote_stream(readings), trace=lambda ts, m: notes.append((ts, m)),
                                                     raw=dict(readings))
             if g.confidence >= 0.6 and g.ply >= 10]
    up = str(info.get("upload_date") or "")
    published = f"{up[:4]}-{up[4:6]}-{up[6:8]}" if len(up) == 8 else ""
    cands = benchmark.candidates_for_video(info.get("channel") or info.get("uploader") or "", published, vid)
    out = []
    for g in games:
        entry = {"moves_san": g.moves_san, "move_seconds": g.move_seconds, "ambiguous_plies": list(g.ambiguous)}
        real, _ = benchmark.match(entry, cands)
        if real is None and not keep_unmatched:
            continue
        # bunches: the plies accepted by one "ply N:" note, with the notes since the previous one
        bunches, seen = [], set()
        lo, hi = g.move_seconds[0] - 30, g.move_seconds[-1] + 1
        for ts, m in notes:
            if not lo <= ts <= hi:
                continue
            mm = PLY.match(m)
            if mm:
                n = int(mm.group(1)) - 1
                if n < len(g.moves_san) and abs(g.move_seconds[n] - ts) < 0.01:
                    bunches.append([n, sorted(seen)])
                seen = set()
                continue
            seen.update(k for k, rx in KINDS if rx.search(m))
        out.append({"video_id": vid, "start_seconds": g.move_seconds[0],
                    "moves_san": g.moves_san, "move_seconds": g.move_seconds,
                    "ambiguous_plies": list(g.ambiguous), "fuzzy_plies": list(g.fuzzy),
                    "correct": benchmark._prefix(g.moves_san, real["moves_san"], g.ambiguous) if real else None,
                    "real_ply": len(real["moves_san"]) if real else None, "bunches": bunches})
    return out


def build(log: Path, vids: list[str], jobs: int, keep_unmatched: bool = False) -> None:
    games = []
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        for i, (vid, out) in enumerate(zip(vids, pool.map(_build_one, vids, [keep_unmatched] * len(vids))), 1):
            games += out
            print(f"[{i}/{len(vids)}] {vid}: {len(out)} matched games", flush=True)
    log.write_text(json.dumps(games))
    print(f"wrote {log}: {len(games)} games")


def doubtful(game: dict, rules: list) -> list[int]:
    starts = game["bunches"] + [[len(game["moves_san"]), []]]
    out = []
    for (a, ctx), (b, _) in zip(starts, starts[1:]):
        if b > a and any(r(set(ctx), b - a) for r in rules):
            out += range(a, b)
    return out


def rough(game: dict) -> list[int]:
    """reconstruct.Game.rough from the log: bunches right after an unexplainable reading a square off."""
    return [a for a, ctx in game["bunches"] if "unreach1" in ctx]


def apply_marks(games: list[dict], catalog: Path) -> tuple[list[dict], int]:
    """Games whose moves match a catalog entry's (same video, start within
    10 s; the entry may be shorter, e.g. catalog.drop_final_bunch), with that
    entry's times and marks and the log's bunch contexts. Others dropped."""
    by_vid: dict[str, list[dict]] = {}
    for e in json.loads(catalog.read_text()):
        by_vid.setdefault(e["video_id"], []).append(e)
    out = []
    for g in games:
        for e in by_vid.get(g["video_id"], []):
            n = len(e["moves_san"])
            if abs(e["move_seconds"][0] - g["start_seconds"]) < 10 and g["moves_san"][:n] == e["moves_san"]:
                out.append({**g, "moves_san": e["moves_san"], "move_seconds": e["move_seconds"],
                            "ambiguous_plies": e.get("ambiguous_plies") or [], "fuzzy_plies": e.get("fuzzy_plies") or [],
                            "correct": None if g["correct"] is None else min(g["correct"], n), "bunches": [b for b in g["bunches"] if b[0] < n]})
                break
    return out, len(games) - len(out)


def replay(log: Path, rule_sets: list[str], clocks: Path | None, marks: Path | None = None) -> None:
    games = json.loads(log.read_text())
    if marks:
        games, dropped = apply_marks(games, marks)
        print(f"times and marks from {marks}: {len(games)} games ({dropped} whose moves differ dropped)")
    has_clocks = set()
    if clocks:
        for e in json.loads(clocks.read_text()):
            if e.get("clock_white_seconds") and e.get("clock_black_seconds"):
                has_clocks.add((e["video_id"], round(e["move_seconds"][0], 2)))
    base = None
    for spec in ["none"] + rule_sets:
        rules = [] if spec == "none" else [RULES[r] for r in spec.split("+")]
        rows = []
        for g in games:
            key = (g["video_id"], round(g["start_seconds"], 2))
            _, t = trust.assess(g["moves_san"], g["move_seconds"], clocks_checked_end=key in has_clocks,
                                ambiguous=g["ambiguous_plies"], fuzzy=g["fuzzy_plies"], doubtful=doubtful(g, rules),
                                rough=rough(g))
            # a game with no real game (build --keep-unmatched) can't be judged wrong
            ok = g["correct"] is None or g["correct"] >= t
            rows.append((key, t, ok, t >= 12, t >= min(30, g["real_ply"] or len(g["moves_san"]))))
        s = [len(rows) - sum(r[2] for r in rows), sum(r[1] for r in rows), sum(r[3] for r in rows), sum(r[4] for r in rows)]
        line = f"{spec:<28} wrong trusted games {s[0]:>3}  trusted plies {s[1]:>6}  shown {s[2]:>5}  openings {s[3]:>5}"
        if base:
            line += f"   ({s[0] - base[0][0]:+d} wrong, {s[1] - base[0][1]:+d} plies, {s[2] - base[0][2]:+d} shown, {s[3] - base[0][3]:+d} openings)"
        print(line)
        if base:
            for (k, t0, ok0, *_), (_, t1, ok1, *_) in zip(base[1], rows):
                if ok0 != ok1:
                    print(f"    {'fixed' if ok1 else 'BROKE'} {k[0]} {k[1]:.0f}: trusted {t0} -> {t1}")
        else:
            base = (s, rows)
            for k, t, ok, *_ in rows:
                if not ok:
                    print(f"    leak {k[0]} {k[1]:.0f}: trusted {t}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("log", type=Path)
    b.add_argument("vids", nargs="+")
    b.add_argument("--jobs", type=int, default=10)
    b.add_argument("--keep-unmatched", action="store_true",
                   help="also keep games with no real game (correct = None): their bunch contexts give a merge its "
                        "doubtful plies; replay can't score them, so don't replay such a log")
    r = sub.add_parser("replay")
    r.add_argument("log", type=Path)
    r.add_argument("rules", nargs="*", help=f"rule names joined by '+': {', '.join(RULES)}")
    r.add_argument("--clocks", type=Path)
    r.add_argument("--marks", type=Path, help="take move times and ambiguous/fuzzy plies from this catalog")
    args = ap.parse_args(argv)
    if args.cmd == "build":
        build(args.log, args.vids, args.jobs, args.keep_unmatched)
    else:
        replay(args.log, args.rules, args.clocks, args.marks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
