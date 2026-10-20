# Handoff: where work is (2026-10-20, 05:20)

Running unattended. Saved state is in `scanner/cache/handoff/` (gitignored).

## Live site / git

- `web/data/games.json` is still the 354-game catalog (76 videos) from 2026-10-15. Nothing new merged yet.
- Pushed up to `d98e47c`. Local, unpushed: `c871b70` onwards (push only when Cat asks).

## Running now: the full rebuild that becomes the merge

Every video rebuilt from its saved readings with the final code (moves, refine, clocks, ratings,
trust), so the merge is one consistent catalog. Three runs, all `tests/rescan.py --from-readings`:

- `handoff/full1/`: 189 videos (`handoff/ab_unreach_vids.txt`), 10 jobs. Started 02:52.
- `handoff/full1b/`: the 5 freshly re-read videos. **Obsolete**: started before the clock fallback.
- `handoff/full1c/`: the 53 videos that started before the clock tallest-line fallback was
  switched on at 05:17 (`full1c/vids.txt`: `full1/before_tallest.txt` + the 5 re-reads), 4 jobs.

**Merge rule:** for each video in `full1c/vids.txt` take `full1c`'s games, else `full1`'s.
Only use a video's part if its run.log line says `ok`. Apply `catalog.drop_final_bunch` then
`catalog.add_trust` to every merged entry, and set `catalog.DROP_FINAL_BUNCH` on by default
(it was kept off so the rebuild stayed one version). Then benchmark (0 wrong trusted moves is the
bar), compare against `merge1/cand.json` + `merge1/bm.txt` (by video + start_seconds), report
gains *and* losses, and for the 14 template re-read videos keep the new games only if they score
better. Then merge into `web/data/games.json` and commit.

Expect trust to show fewer games than `merge1` (the back-off below: −4.5% shown, −12.6% openings in
replay) and clocks to be much better (lichess especially).

## Done this session (all committed)

- `tests/trust_replay.py`: trust rules replayed from a once-built log in seconds (was ~2 h per A/B).
  Log: `handoff/replay_log.json` (1,620 games; rebuild after any reconstruct change).
- `trust.ROUGH_BACKOFF = 5`: where trust ends within 5 plies after a bunch accepted right after an
  unexplainable reading a square off, it ends at that bunch. Replay: wrong trusted games 9 → 0.
- Clocks: in-process Tesseract (4× faster a call, identical reads); last fallback reads only the
  tallest text line (lichess boxes taking in the time bar and name row).
- Ratings: hi-res frames cached in `scanner/cache/hires/` (no network on rescans).
- `FFMPEG_THREADS` caps ffmpeg's decode threads (default ~5 a decode). Use 2 in parallel runs.
- `tests/sets/hard.txt` + `guard.txt`, and "Iterating fast" in EXPERIMENTS.md.

## Targets (Cat, 2026-10-20)

Share of matched games passing each `tests/benchmark.py` check: **moves 95%** (every move right),
**ratings 80%** (both ±20), **clocks 80%** (95% of values ±2s). Separately, 0 games with a wrong
trusted move is the hard bar for merging. On 168 rebuilt videos: moves 73.1%, ratings 77.6%
(lichess 52.7%), clocks 83.4%.

## Next

- **Alongside scanner batches: the assistant prototype** (`ASSISTANT.md`, "First steps" 1-3).
  Measuring coverage, aligning transcripts to positions and a hand-distilled summary of the
  densest positions need almost no CPU, so they run while a batch does.

- lichess ratings: read fine from the right clock boxes (checked on a frame); the misses should
  mostly go with the clock fixes. Measure on the rebuild before working on them.
- Keep total jobs at or below 16 cores.

## Transcripts

`scanner/cache/transcripts/<video_id>.json` (local only, not in git), saved by `scan.py` for every new video. Fetch one at a time
(YouTube answers 429 to parallel requests).
