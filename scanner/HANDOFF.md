# Handoff: where scanning stopped (2026-10-15)

Work was stopped on purpose at 13:40 on 2026-10-15. Nothing is running and nothing is broken.
Saved state lives in `scanner/cache/handoff/` (gitignored):

- `batch2_ids.json`: the 141 new tournament videos
- `to_reconstruct.txt`: the 123 of them not yet built into catalog entries
- `refit_ids.txt`, `old_readings/`: the 18 library videos re-read after the re-fit gate (done and merged)
- `batch2_logs/`, `refit_logs/`: read logs
- `scripts/`: scratch scripts; `cmp_catalogs.py` compares two benchmarked catalogs game by game,
  matched by (video, start_seconds)

## Live site / git

- `web/data/games.json`: **354 games from 76 videos**, 241 listed on the site (12+ trusted plies).
  It was 221 games from 59 videos, 158 listed.
- Benchmark: 337 matched, **0 wrong trusted plies of 8,831**, whole opening trusted in 45%.
- Merged this session: the 18 re-read library videos (every-move-right 55 -> 61 games, shown 60 -> 66,
  0 wrong trusted) and the first 18 batch videos to finish reconstructing (17 had games).
- Committed this session: 6e147a1 (`FUZZY_CONFIRM_S` = 2.0) and the catalog update. Nothing is pushed.

## 1. Build the remaining 123 batch videos (just CPU, no downloads)

Readings exist for 140 of 141 batch videos, so this only reruns reconstruction.
It is slow: about 45 minutes per video per job, so ~10 hours at 10 jobs.

```sh
scanner/.venv/bin/python scanner/tests/rescan.py --from-readings --jobs 10 \
  --out /tmp/batch/games.json $(cat scanner/cache/handoff/to_reconstruct.txt)
```

The video without readings is `8y9gWaB8zk4` (YouTube 403 during the batch, the retry was stopped).
Its `.webm` may be partial: delete `scanner/cache/8y9gWaB8zk4.*` and run `tests/dump_batch.py` on it alone.

rescan.py seeds each per-video part file with `[]`, so a killed run's parts are only valid for
videos its log marks `ok`. To merge early, collect those parts yourself (that's what was done this time).

Then, as before:
1. `scanner/tests/merge_catalog.py /tmp/batch/games.json --only <ids>` (into a copy of the live catalog first)
2. `scanner/catalog.py --catalog <copy>`, then `scanner/tests/benchmark.py --catalog <copy> --json ...`
3. Hard bar: 0 wrong trusted. Copy over `web/data/games.json`, commit.

## 2. Open trust leak: Aig9-ZyQ8Ak game 4 (start 1393 s)

Left OUT of the live catalog. 25 plies recorded, first 13 right, but 17 trusted: plies 14-17 are wrong
and trusted. First wrong trusted game since the trust recalibration. Trace it with
`scanner/tests/trace_video.py` before the next merge; when building that video again,
check whether the leak is still there before including the game.

## 3. Done this session

- Fuzzy-confirm A/B: 0.0 vs 2.0 on 41 library videos, identical moves, 0 wrong trusted either way;
  trusted plies 3,345 -> 3,567, shown 95 -> 100, whole openings 51 -> 59. Default set to 2.0 (6e147a1).
- The 6 batch videos read before the re-fit gate: their board shifts are all 2-8 px, no bad re-fits.
- Library video `9dd8dmyDkU8`: the re-read found its one game below the 0.6 confidence cutoff,
  so the live catalog keeps the old entry.

## 4. After that

- Hidden games (fewer than 12 trusted plies): mostly cut before fuzzy bunches, big/late bunches, ambiguous routes.
- Keep adding tournament videos (Titled Tuesday and similar, not speedruns).
- See EXPERIMENTS.md "Current state" before trying any idea.
