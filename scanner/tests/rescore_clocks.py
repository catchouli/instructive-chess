"""Re-run only the clock stage for catalogued games and score old vs new
against the real games (see benchmark.py) - minutes instead of a full rescan,
since a game's moves no longer depend on its clocks.

    scanner/.venv/bin/python scanner/tests/rescore_clocks.py <game_id> [<game_id> ...]
    scanner/.venv/bin/python scanner/tests/rescore_clocks.py --matched   # every benchmark-matched game
    ... --write   also store the new clocks in web/data/games.json

Needs each game's video in scanner/cache/.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import benchmark  # noqa: E402
import catalog    # noqa: E402
import scan       # noqa: E402


def _video(video_id: str) -> Path | None:
    return next((p for p in benchmark.VIDEO_CACHE.glob(video_id + ".*")
                 if p.suffix in (".mkv", ".webm", ".mp4")), None)


def _clock_line(res: dict) -> str:
    c = res["clock"]
    return f"{c['within']:3}/{c['checked']:<3} ok, {c['missing']:3} missing, {c['wrong']:3} wrong"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--matched", action="store_true", help="every game the benchmark can match")
    ap.add_argument("--write", action="store_true", help="store the new clocks in the catalog")
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

    tot_old = tot_new = 0
    for gid in ids:
        g = by_id[gid]
        real = real_for(g)
        path = _video(g["video_id"])
        if real is None or path is None:
            print(f"{gid:<18} skipped ({'no real game' if real is None else 'video not cached'})")
            continue
        old = benchmark.score(g, real)
        start, end = max(0.0, g["start_seconds"] - 1), g["move_seconds"][-1] + 3
        box = scan.board_detect.detect(scan.sample_frames(path, start, end, 15))
        if box is None:
            print(f"{gid:<18} skipped (board not found)")
            continue
        game = SimpleNamespace(move_seconds=g["move_seconds"], start_seconds=g["start_seconds"],
                               flipped=g["flipped"], clock_white_seconds=[], clock_black_seconds=[])
        scan.annotate_clocks(path, box, [game])
        new_g = dict(g)
        for side in ("white", "black"):
            vals = getattr(game, f"clock_{side}_seconds")
            new_g.pop(f"clock_{side}_seconds", None)
            if vals:
                new_g[f"clock_{side}_seconds"] = catalog._clock_seconds(vals)
        new = benchmark.score(new_g, real)
        tot_old += old["clock"]["ok"]
        tot_new += new["clock"]["ok"]
        print(f"{gid:<18} old {_clock_line(old)}   new {_clock_line(new)}"
              f"{'   <- now OK' if new['clock']['ok'] and not old['clock']['ok'] else ''}"
              f"{'   <- REGRESSED' if old['clock']['ok'] and not new['clock']['ok'] else ''}", flush=True)
        if args.write:
            by_id[gid].clear()
            by_id[gid].update(new_g)
    print(f"\ngames with clocks OK: {tot_old} -> {tot_new}")
    if args.write:
        tmp = catalog.DATA_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(games, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(catalog.DATA_FILE)
        print(f"wrote {catalog.DATA_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
