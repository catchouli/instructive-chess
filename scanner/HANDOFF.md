# Handoff: where work stopped (2026-10-19, 17:56)

Stopped on purpose for the day. Nothing is running and nothing is broken.
Saved state is in `scanner/cache/handoff/` (gitignored).

## Live site / git

- `web/data/games.json` is still the 354-game catalog (76 videos) from 2026-10-15. Nothing new is merged yet.
- All commits up to `d98e47c` are pushed; this note's commit is local.
- Committed today: clock widened-crop retry, arena-timer rejection, same-rating rule, missing-king
  castling fix, template re-cut at box re-fits (on by default), transcripts (75 of 76 videos),
  MIT license, and the "doubtful plies" trust code with both rules **off** (see 3).

## 1. The big merge is ready except for trust leaks

`handoff/merge1/cand.json` = live + all 122 remaining batch videos: **1,768 games from 194 videos**,
1,254 listed. Benchmark (`merge1/bm.txt`), 1,652 matched:

| | all | chess.com | lichess |
|---|---|---|---|
| every move right | 66% | 63% | 74% |
| clocks | 64% | 80% | 31% |
| ratings | 74% | 86% | 50% |

**Blocker: 10 games with wrong trusted moves** (60 of 50,125 trusted plies). Hard bar is 0.
If trust can't be fixed quickly, merge without those games, as was done for Aig9-ZyQ8Ak-4.

Before merging, re-read the clocks of every game with the final clock code
(`tests/rescore.py --no-ratings --write --out ...`): the batch was built while the clock fixes
landed, and some of it used a too-strict stand-still test that dropped real clocks.

## 2. Template re-cut re-reads (14 videos, 9 done)

`handoff/reread_ids.txt` are the batch videos with the most missing kings. 9 have fresh readings in
`scanner/cache/` (template re-cut on); their old readings are in `handoff/old_readings2/`.
The other 5 (`handoff/reread_remaining.txt`) were stopped mid-read; their **old** readings were put
back in `scanner/cache/`. To finish them, move those 5 readings files aside again and run:

```sh
scanner/.venv/bin/python scanner/tests/dump_batch.py --jobs 4 --log scanner/cache/handoff/reread_logs $(cat scanner/cache/handoff/reread_remaining.txt)
```

Then rebuild all 14 (`tests/rescan.py --from-readings`) and keep each video's new games only if
they score better than its games in `merge1/cand.json` (compare by video + start_seconds).

## 3. Trust leaks: what's measured

`handoff/scripts/context_stats.py` counted what happened just before each accepted ply
(194 videos, ~100k plies). First-wrong rate: nothing unusual 0.3%, unexplainable reading a square
off 1.2%, undo 3.4%, browse back 6%, detour retract 35%.

- `reconstruct.RETRACT_ENDS_TRUST` (moves right after undo/browse/detour become doubtful):
  A/B on 194 videos (`handoff/ab_retract.log`): wrong trusted games 9 → 6, but whole openings
  727 → 682 and shown 1,064 → 1,048. **Too costly as is.** Next: try it without undo (the most
  common, least risky event), and let `refine.py` clear doubtful marks on bunches it splits.
- `reconstruct.UNREACH_BUNCH` (a 2+ bunch right after an unexplainable reading a square off
  becomes doubtful): covers 5 of the 6 remaining leaks. Its A/B was stopped unfinished; rerun:

```sh
VALS=0,1 JOBS=10 scanner/.venv/bin/python scanner/cache/handoff/scripts/ab_par.py scanner/cache/handoff/ab_unreach UNREACH_BUNCH $(cat scanner/cache/handoff/ctx/vids.txt)
```

  (`ab_par.py` sets only the named flag; set the other in `reconstruct.py` to the value you want
  held fixed. About two hours on 10 cores.)

The remaining leaks (reconstruct-only arms): PEpvyA_2kG4 6963, Wl1gISrGdxw 275 and 5517,
YSOpxOs2zLs 3892 (a single move, not a bunch), cure2iLB6KI 103, dLDFs8iiEUo 667.

## 4. Next accuracy targets

- lichess clocks (31%) and ratings (50%): not looked at yet. Start by looking at frames.
- Keep every job count at or below the core count: 16 cores. Two batches at once made everything slower.

## 5. Transcripts

`transcripts/<video_id>.json`, saved by `scan.py` for every new video. Fetch one at a time:
YouTube answers 429 to parallel caption requests. Future idea (not started): a cleaned per-move view
using the catalog's moves to correct notation and tag off-topic chatter.
