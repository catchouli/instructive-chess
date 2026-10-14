# Handoff: where scanning stopped (2026-10-14)

All background jobs were stopped on purpose at 17:23 on 2026-10-14 to free the machine.
Nothing is broken, and everything below can be resumed.

Saved state lives in `scanner/cache/handoff/` (gitignored, so it isn't in git):

- `batch2_ids.json`: the 141 new tournament videos being read
- `refit_ids.txt`: the 18 library videos to re-read
- `old_readings/`: those 18 videos' readings from before the re-fit gate
- `batch2_logs/`: logs from the batch so far
- `ab_fuzzyconfirm.log`: the half-finished A/B
- `scripts/`: scratch A/B and analysis scripts (`ab_val.py`, `trust_stats.py`, `retrust.py`, `rvt*.py`)

## Live site / git

- The live catalog `web/data/games.json` has 221 games from 59 videos.
  The site hides games with fewer than 12 trusted plies (`MIN_SHOWN_PLY`), which hides 63 of them.
- The last benchmark found 0 wrong trusted moves, and every move was right in about 71% of games.
- Local commits not pushed yet: b61ded7, c421d8b, 0051404, 1020d21, plus this note.

## 1. Finish the batch read (biggest win for library size)

The batch had read 33 of 141 videos (5 hours with 6 jobs, about 16 hours left at that rate).
Finished videos have `scanner/cache/<id>-readings.json` and are skipped on restart.
The 6 videos in progress when it stopped are read again from scratch.

```sh
scanner/.venv/bin/python scanner/tests/dump_batch.py --jobs 6 \
  --log scanner/cache/handoff/batch2_logs/logs $(python3 -c "import json;print(' '.join(json.load(open('scanner/cache/handoff/batch2_ids.json'))))")
```

Use `--jobs 2` or `3` if the machine is needed at the same time.

The first 6 batch videos (`_zT8aWZh2x0`, `y5eb3xaRRPA`, `_ZmPtTJHfNI`, `5M0c_EwRQiI`, `iFdoGDFnT3c`, `0RpvhiIZQkk`) were read before the re-fit gate (c421d8b).
To check them, look in their logs for big "board moved" jumps.
Shifts of 2 to 4 px are normal; a jump of tens of px or a size change means a bad re-fit.
If you find one, delete that video's readings file and read it again.

## 2. Re-read the 18 library videos where the old re-fit fired

```sh
scanner/.venv/bin/python scanner/tests/dump_batch.py --jobs 6 --log scanner/cache/handoff/refit_logs $(cat scanner/cache/handoff/refit_ids.txt)
```

Their old readings were moved to `handoff/old_readings/`, so `dump_batch` reads them again.
If you'd rather skip this, copy them back to `scanner/cache/`.

## 3. Build catalog entries, merge, benchmark

1. Run `scanner/rescan.py --from-readings <ids>` over the new and re-read videos, writing to a scratch catalog.
2. Run `scanner/tests/merge_catalog.py SCRATCH --into web/data/games.json --only <ids>`.
3. Run `scanner/catalog.py web/data/games.json`.
4. Run `scanner/tests/benchmark.py`. The hard bar is 0 wrong trusted moves.
   Compare games against the old catalog by (video, start_seconds), not by game ID.
5. Commit, and add a row to EXPERIMENTS.md.

## 4. Fuzzy-confirm A/B (stopped halfway)

`reconstruct.FUZZY_CONFIRM_S` is committed at 0.0 (off).
The idea: trust a fuzzy bunch once its resulting position is then read exactly for 2+ seconds.
In 209 games, all 7 wrong early fuzzy bunches never had their position read exactly, while 24 right ones did.

The baseline arm finished: 132 matched, 81 every move right, 0 wrong trusted, 3,345 trusted plies, 95 shown, 51 whole openings.
The 2.0 arm was not run.

To rerun only that arm, use the same library videos that have readings (47 of them):

```sh
VALS=2 scanner/.venv/bin/python scanner/cache/handoff/scripts/ab_val.py <outdir> FUZZY_CONFIRM_S <video ids...>
```

It takes about 1 to 2 hours on one core.
If the result has 0 wrong trusted games and more games shown than 95, set the default to 2.0 and commit.
Fuzzy bunches are the main reason games are hidden (32 of the 63).

## 5. After that

- Keep fixing hidden games. Of the 63:
  - 32 are cut before a fuzzy bunch
  - 13 are cut before a big or late bunch
  - 10 are cut before an ambiguous route
  - 8 are genuinely short
- Keep adding tournament videos (Titled Tuesday and similar, not speedruns).
- See EXPERIMENTS.md "Current state (2026-10-14)" before trying any idea.
