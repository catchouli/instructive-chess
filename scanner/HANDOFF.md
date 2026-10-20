# Handoff: where work is (2026-10-20, evening)

Saved state is in `scanner/cache/handoff/` (gitignored).

## Live site

`web/data/games.json` is the 2026-10-20 rebuild: **1,774 games from 193 videos, 1,187 listed**
(12+ trusted plies). It was 354 games (76 videos, 241 listed) before. Benchmark
(`handoff/merge2/bm.txt`), 1,653 games matched to a real game:

| | before (merge1 candidate) | now |
| --- | --- | --- |
| Wrong trusted (shown) games | 10 | **0** (0 of 44,588 trusted plies) |
| Every move right | 72.7% | 72.8% |
| Clocks (95% of values ±2s) | 63.9% | **82.5%** (lichess 30.4% → 74.5%) |
| Ratings (both ±20) | 74.2% | 76.9% (lichess 55.4%) |
| Listed games | 1,227 | 1,187 (−3.3%) |
| Whole 15-move openings trusted | 886 | 736 (−17%) |
| Trusted plies | 50,090 | 44,588 (−11%) |

The losses are the two new trust rules (rough back-off, undo/detour), the price of 10 → 0.
No game had to be hidden: the full benchmark found no leak.

How it was built: `handoff/scripts/merge2.py` (full1c's games for `full1c/vids.txt`, else
full1's, ok parts only; undo/detour `doubtful_plies` from the replay logs `replay_log2*.json`,
since the rebuild ran before that rule was on; then `drop_final_bunch` and `add_trust`).
`catalog.DROP_FINAL_BUNCH` and `reconstruct.RETRACT_ENDS_TRUST` (undo/detour, not browsing) are
now on by default, so new scans produce the same.

## Left over from the merge

- **7 template re-read videos scored worse** on every move right with their new readings:
  ptZI7tREx_Y 4 → 2, 9vQg9P0gUmU 4 → 2, uXOKa40Ie6k 5 → 3, iAkCsXSxBmg 5 → 3,
  8C69k-ce4-c 5 → 2, b97fezBKqpc 7 → 3, F6fw2jek4Xk 3 → 1. The new readings went in anyway:
  the old games predate the marks the trust rules need. Next: rebuild these 7 from their old
  readings (`handoff/old_readings2/`, `old_readings3/`) with current code and keep whichever
  version scores better.
- `OFbL_VUP8Nw` produces no games (none before either).
- `web/data/games.json` is 9 MB pretty-printed (1.4 MB gzipped, which Pages serves). Writing it
  compact, or leaving out fields the site doesn't use, would halve it.
- Matches to real games aren't saved yet: write `scanner/cache/<video_id>-truth.json`
  (video, start, first moves, real game URL) from `benchmark.py --json` (Cat wants these kept).

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
