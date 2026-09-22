"""Re-run only the clock and rating stages for catalogued games and score old
vs new against the real games (see benchmark.py) - minutes instead of a full
rescan, since a game's moves don't depend on either.

    scanner/.venv/bin/python scanner/tests/rescore.py <game_id> [<game_id> ...]
    scanner/.venv/bin/python scanner/tests/rescore.py --matched   # every benchmark-matched game
    ... --no-ratings   clocks only (ratings fetch a high-res frame from YouTube)
    ... --write        also store the new clocks/ratings in web/data/games.json

Needs each game's video in scanner/cache/.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import benchmark  # noqa: E402
import catalog    # noqa: E402
import download   # noqa: E402
import scan       # noqa: E402


def _video(video_id: str) -> Path | None:
    return next((p for p in benchmark.VIDEO_CACHE.glob(video_id + ".*")
                 if p.suffix in (".mkv", ".webm", ".mp4")), None)


def _moves_line(res: dict) -> str:
    return f"mv {res['moves_correct']:3}/{res['ply']:<3}"


def _clock_line(res: dict) -> str:
    c = res["clock"]
    return f"clk {c['within']:3}/{c['checked']:<3} ({c['missing']} miss, {c['wrong']} wrong)"


def _rating_line(res: dict) -> str:
    return "rtg " + " ".join(f"{side[0]}:{v['got']}/{v['truth']}" for side, v in res["rating"].items())


def _rescore_one(g: dict, no_ratings: bool) -> tuple[dict | None, str]:
    """Rerun the clock (and rating) stage for one catalogued game; returns
    the updated entry, or None and why it was skipped. Runs in a worker
    process."""
    path = _video(g["video_id"])
    if path is None:
        return None, "video not cached"
    start, end = max(0.0, g["start_seconds"] - 1), g["move_seconds"][-1] + 3
    box = scan.board_detect.detect(scan.sample_frames(path, start, end, 15))
    if box is None:
        return None, "board not found"
    game = SimpleNamespace(moves_san=list(g["moves_san"]), move_seconds=list(g["move_seconds"]),
                           start_seconds=g["start_seconds"], flipped=g["flipped"],
                           clock_white_seconds=[], clock_black_seconds=[],
                           clock_boxes=[], rating_white=None, rating_black=None)
    scan.annotate_clocks(path, box, [game])
    new_g = dict(g)
    # the clock stage drops post-game moves (clock_read.live_move_count)
    new_g["moves_san"], new_g["move_seconds"] = list(game.moves_san), list(game.move_seconds)
    new_g["ply"] = len(game.moves_san)
    new_g["pgn"] = scan.reconstruct.Game(start_seconds=g["start_seconds"], moves_san=list(game.moves_san)).pgn()
    for side in ("white", "black"):
        vals = getattr(game, f"clock_{side}_seconds")
        new_g.pop(f"clock_{side}_seconds", None)
        if vals:
            new_g[f"clock_{side}_seconds"] = catalog._clock_seconds(vals)
    if not no_ratings:
        src = download.Source(video_id=g["video_id"], path=path, title="", channel=g["channel"],
                              published=g.get("published") or "", url=g["video_url"], duration=0.0)
        scan.annotate_ratings(src, box, [game])
        for side in ("white", "black"):
            new_g.pop(f"rating_{side}", None)
            if getattr(game, f"rating_{side}") is not None:
                new_g[f"rating_{side}"] = getattr(game, f"rating_{side}")
    return new_g, "ok"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--matched", action="store_true", help="every game the benchmark can match")
    ap.add_argument("--no-ratings", action="store_true", help="rescore clocks only")
    ap.add_argument("--write", action="store_true", help="store the new clocks/ratings in the catalog")
    ap.add_argument("--out", type=Path, help="with --write: write the updated catalog here instead")
    ap.add_argument("--jobs", type=int, default=6, help="games rescored in parallel (default 6)")
    args = ap.parse_args(argv)

    games = json.loads(catalog.DATA_FILE.read_text())
    by_id = {g["id"]: g for g in games}
    ids = list(args.ids)
    cand_cache: dict[str, list] = {}

    def real_for(g):
        vid = g["video_id"]
        if vid not in cand_cache:
            cand_cache[vid] = benchmark.candidates_for_video(g["channel"], g.get("published"), vid)
        return benchmark.match(g, cand_cache[vid])[0]

    if args.matched:
        ids += [g["id"] for g in games if g["id"] not in ids and real_for(g) is not None]
    reals = {gid: real_for(by_id[gid]) for gid in ids}

    # Tesseract's own threading would oversubscribe the cores across workers.
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    totals = {"moves": [0, 0], "clocks": [0, 0], "ratings": [0, 0]}
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(_rescore_one, by_id[gid], args.no_ratings): gid
                   for gid in ids if reals[gid] is not None}
        for gid in ids:
            if reals[gid] is None:
                print(f"{gid:<18} skipped (no real game)")
        for fut in as_completed(futures):
            gid = futures[fut]
            try:
                new_g, why = fut.result()
            except Exception as e:  # one bad video shouldn't lose the whole run
                print(f"{gid:<18} FAILED: {e!r}", flush=True)
                continue
            if new_g is None:
                print(f"{gid:<18} skipped ({why})", flush=True)
                continue
            g, real = by_id[gid], reals[gid]
            old, new = benchmark.score(g, real), benchmark.score(new_g, real)
            flags = []
            for key, label in (("moves", "moves"), ("clocks", "clocks"), ("ratings", "ratings")):
                was = {"moves": old["moves_ok"], "clocks": old["clock"]["ok"], "ratings": old["rating_ok"]}[key]
                now = {"moves": new["moves_ok"], "clocks": new["clock"]["ok"], "ratings": new["rating_ok"]}[key]
                totals[key][0] += was
                totals[key][1] += now
                if was != now:
                    flags.append(f"{label} now OK" if now else f"{label} REGRESSED")
            print(f"{gid:<16} old {_moves_line(old)} {_clock_line(old)} {_rating_line(old)}\n"
                  f"{'':<16} new {_moves_line(new)} {_clock_line(new)} "
                  f"{_rating_line(new)}  {' '.join('<- ' + f for f in flags)}", flush=True)
            if args.write:
                by_id[gid].clear()
                by_id[gid].update(new_g)

    n = len(futures)
    print(f"\n{n} games rescored")
    print(f"every move right: {totals['moves'][0]} -> {totals['moves'][1]}")
    print(f"clocks OK:        {totals['clocks'][0]} -> {totals['clocks'][1]}")
    if not args.no_ratings:
        print(f"ratings OK:       {totals['ratings'][0]} -> {totals['ratings'][1]}")
    if args.write:
        out = args.out or catalog.DATA_FILE
        tmp = out.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(games, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(out)
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
