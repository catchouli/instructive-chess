# Scanner experiments log

What's been tried to make the scanner more accurate, what it measured, and
whether it was kept. Read this before trying an idea, since several obvious
ones have already failed for non-obvious reasons. Add to it as you go.

## Requirements and roadmap (updated 2026-10-13)

What the catalog is for: finding instructive games by their opening moves,
where the *narrator* (the master whose channel it is) is the one playing the
line you looked up. Requirements from the user, in priority order:

1. **Never show a wrong move** (trusted plies; see `trust.py`). Target for
   every reading - moves, clocks, ratings - is 95%+ of games right.
2. **Narrator side ("instructive POV")**: record which colour the narrator
   played in every game, show it on each card, and let the user filter by
   it. The point: looking up an opening and finding a weak speedrun
   *opponent* is the one playing it is useless. Derived from board
   orientation (`flipped`: a player's own live board has their side at the
   bottom) - verified against the real games' player names by
   `tests/benchmark.py` (POV row). Stored as `pov` ("white"/"black").
3. **Filters on the site**: narrator side (any / white / black), minimum
   rating, and "exclude speedruns". A game with no readable rating is
   excluded by an active rating filter rather than shown as a false match.
4. **Expand the catalog** - preferring **Titled Tuesday and other
   high-level tournament videos** over speedruns (speedrun opponents are
   weak, so the opponent's half of the game isn't instructive). Each video
   is tagged `speedrun: true/false` from its title ("Speedrun", "Beginner
   To Master", "Climbing the Rating Ladder"...); `scan.py --speedrun /
   --no-speedrun` overrides.
5. Expand the test suite along with the catalog: every new video with
   ground truth (linked games or a known account - see `ACCOUNTS` in
   `tests/benchmark.py`) grows the benchmark. Recheck the trust rule on
   the bigger library (0 wrong trusted plies is the bar).

Candidate channels and accounts for tournament videos: Eric Rosen
(`imrosen`; many 2h "Facing the World's BEST Chess Players in Titled
Tuesday" streams), Daniel Naroditsky (`danielnaroditsky`), John Bartholomew
(chess.com `Fins`), GingerGM / Simon Williams (chess.com `GingerGM`).

## How things are measured

- `tests/benchmark.py` matches each catalogued game to its real game (linked
  from the video description, or found in the channel's chess.com archives)
  and scores moves, clocks and ratings. The "Trusted moves" section scores
  what the web app actually shows.
- `tests/rescan.py` rescans videos in parallel into a scratch catalog, so a
  change can be benchmarked before it replaces `web/data/games.json`.
  **Don't edit scanner code while it runs**: each video starts a fresh
  `scan.py`, so an edit mixes versions.
- `tests/rescore.py` reruns only the clock/rating stages (faster), but must
  use the calibrated board box (`_calibrated_box`) or ratings come out wrong.
- Compare a change on a few affected videos plus controls first, then on the
  whole library. Several ideas that helped on 3 videos were neutral or worse
  on all 52.

### Iterating fast (2026-10-20)

Use the cheapest stage that a change touches; run the whole library only before a merge.

| change to | how | time |
|---|---|---|
| trust rules (what's trusted, not which moves) | `tests/trust_replay.py replay LOG` on a log built once with `build` (rebuild the log after any reconstruct change) | seconds, all games |
| reconstruct | `ab_par`-style A/B over cached readings on `tests/sets/hard.txt` + `guard.txt` | ~20-40 min |
| clocks | `tests/rescore.py --no-ratings --catalog C` on the failing games' ids | minutes |
| ratings | `tests/rescore.py --catalog C`: hi-res frames come from `cache/hires/` after the first fetch | minutes |
| board reading | `tests/dump_batch.py` (re-reads video), then rebuild | hours: batch these |

`tests/sets/hard.txt`: the videos of every known failure (the trust leaks, the worst videos per
platform for moves, clocks and ratings). `tests/sets/guard.txt`: 20 videos sampled by channel
(seed 20261020) to catch regressions. Keep both fixed so results stay comparable over time.
Cap decoder threads in parallel runs (`FFMPEG_THREADS=2`, `OMP_THREAD_LIMIT=1`) and keep the
job total at or below the cores (16): ffmpeg takes ~5 cores a decode by default.

### Caching a rescan (2026-10-26)

A `--from-readings` rescan keeps what it decoded or read in `scanner/cache/`, so the next run does
only the stages whose code changed. Each cache is keyed by the board box and a fingerprint of the
source of the modules that produced it (`scan.code_stamp`), so a code change re-reads instead of
reusing stale results.

| cache | holds |
|---|---|
| `VID-dense.json` | 6 fps board readings on a fixed grid (k/6 s), shared by the stall re-read and the fast-move re-timing (`refine.DenseFrames`); adopts the frames of the older `VID-stalls.json` |
| `VID-clocks.json` | clock regions and readings per window (windows on whole seconds) |
| `VID-model.npz` | the piece templates, so board detection and the whole-video decode are skipped |
| `VID-ratings.json` | every rating read, per hi-res frame and box shift |

Reconstruction also got faster: a search updates the distance to the reading on the 2-4 squares a
move changes instead of re-comparing all 64, and a search repeated within one run (the stall
re-read reconstructs up to three times) is answered from memory.

A run with the caches filled gives exactly the same games as one without them (checked on
TaM0A-J2dQY and 3Qw4AL8RDOw; reconstruction alone on 12 videos). Times per video: TaM0A-J2dQY
387 s -> 47 s, 3Qw4AL8RDOw 1,091 s -> 90 s; reconstruction on the 12 videos 1,215 s -> 538 s.
The first run after the change fills the caches and costs as much as before.

Measured on the whole library (547 videos, 14 jobs): the run that filled the caches took 11 h 45 min,
the same code again **39 min**, with all 2,599 games identical. Rules that only use clock readings live
in `game_end.py`, not `clock_read.py`, because a change to `clock_read.py` re-reads every clock.

Exception to "don't edit while it runs": the 2026-10-20 full rebuild (`handoff/full1`) took
in-process Tesseract and the hi-res frame cache mid-run; both give identical output (checked).

## Current state (2026-10-21)

Live catalog rebuilt from cached readings with derail recovery and the unsettled-tail drop:
**1,776 games from 193 videos, 1,180 listed**, 1,650 matched. Every move right **83.2%** (was
72.8%; lichess 86.1%, chess.com 81.8%), clocks 83.0%, ratings 77.0% (lichess 55.0%), **0 wrong
trusted moves of 44,394**, plies before the first error 95.5%. Costs against the 2026-10-20
catalog: trusted plies 44,588 -> 44,394, listed 1,187 -> 1,180, whole openings 736 -> 735. Then the lichess
ratings fix: ratings 77.0% -> 81.8% (lichess 55.0% -> 70.3%), wrong rating sides 129 -> 138.

## Current state (2026-10-15)

Live catalog: **354 games from 76 videos**, 241 listed (12+ trusted plies), 337 matched: **0 wrong trusted of 8,831 plies**, whole opening trusted in 45%. Added: the 18 library videos re-read after the re-fit gate, and 18 of the 141-video tournament batch (the rest were read but not yet reconstructed). One new game (Aig9-ZyQ8Ak game 4) had 4 wrong trusted plies (14-17) and was left out; not yet traced.

## Earlier state (2026-10-14)

Live catalog: **220 games from 59 videos** (the 51 earlier videos, all but
one rescanned with the blitz-stream fixes, plus eight Titled Tuesday
streams by Rosen, Naroditsky, Bartholomew and GingerGM), 209 matched to
real games: **0 wrong trusted moves** (of 5,458 trusted plies), the whole
15-move opening trusted in 43%, every move right in 70%, clocks right in
78%, both ratings right in 80%, the narrator's colour detected right in
100%. Correct plies 8,439 → 8,992 on the games the library had before.

Open after the rescan: clocks fell from 86% to 78% of games - 13 games
lost it, mostly games that are now much longer (their late clock values
are wrong) and ~15 speedrun games that got *shorter*: a piece hovered
over a square for a second or two now survives the 3-frame vote and a
fuzzy single move from it starts a self-consistent wrong chain (traced:
97PgPqdci_U game 2 "Bc4" for Bb5+ Bd7 Bc4, 77 → 4 right plies;
Re5ZPpEMPHk game 1, 75 → 15). Requiring the next reading to follow from
the fuzzy move didn't catch them (the chain explains the next frames too).
Net the rescan gained plies in 29 games and lost them in 15.

## Earlier state (2026-09-24)

Live catalog (2026-09-25), 136 of 141 games matched to real games: **0 wrong
trusted moves** (of 4,630 trusted plies), 68% of recorded plies trusted, full
15-move opening trusted in 73 games, every move right in 67% (91 games),
clocks right in 89%, both ratings right in 74%. (On 2026-09-22: 41 games had a wrong move shown,
clocks 13%, ratings 52%.)

## Moves

| Idea | Result | Kept? |
|---|---|---|
| Trim moves after both clocks stop (post-game analysis) | Biggest single class of move errors (17/27); fixed most | Yes (`9edf182`) |
| ...only a clock going *down* counts as running | A clock rising after the game (433→439) stopped the trim early | Yes (`770605d`) |
| ...a mate only ends the game if a clock ran just before it | An analysis line ending in `Rxf8#` was kept as the real ending | Yes (`57ce6b7`) |
| Ignore readings equal to an earlier position (move-list browsing) | D2Gs5upS1AA 9/71 → 67/67 plies right; nothing worse | Yes (`8cf8ffc`) |
| Retract a shown-and-undone line (bunched loop back to an earlier position) | JZ9dFsuc3fE 108 → 188 plies right | Yes (`d6a7993`) |
| ...also retract to wherever browsing went back to | Fixed one video, broke a game in two others | **No** |
| Clock-based move revalidation ("every move costs the mover time") | False for each side's first move and for premoves; with true clocks it rejected a whole game | **Removed** (`954b3aa`) |
| Drop games under 10 plies | All 13 games of 2–5 plies were junk; shortest real game 15 | Yes (`ec1f763`) |
| Whole-video 2 fps | Split fast moves, but halved `vote_stream`'s flicker window (it's in frames); 13 games lost every-move-right vs 8 gained; 7 wrong trusted | **No** (`b6bc724`) |
| Re-read only fast-move moments at 6 fps, timing the known moves (moves never change) | Trusted plies 60% → 73% | Yes (`9f9488f`) |
| ...with a partial split allowed | A 4-ply bunch split 1+1+2 left a trusted wrong pair | **No**, all-or-nothing (`57ce6b7`) |
| ...second pass at 15 fps for unsplit bunches | No change. What's left is premoves, whose in-between position is never drawn | **No** |
| ...compare only the squares the moves change, exactly | +20 trusted plies on 3 videos, −62 on the whole library | **No** (`3f4c9ae`) |
| Board-box tracking (re-align when confidence drops) | Thrashed (60 re-aligns on one video), lost a game. The board hadn't moved | **No** |
| Re-read a poorly matching occupied square at small offsets (±4 px) | A bishop read as empty matched at 0.83 instead of 0.14. On 3 videos: KfuyDm5uksI 3 → 9 games found, lAniuIChYuA-1 all 96 plies right (was 83/88), trusted plies 168 → 375, still 0 wrong | Committed (`7905b60`), see next rows |
| ...on the whole library (loose: any shift, match ≥ 0.42) | 123 → 137 games found, +340 plies captured, but **6 games got wrong trusted moves**: an empty square's edge sliver of a neighbouring piece, shifted towards it, matched a template (phantom pieces) | **No** as-is |
| ...strict: shifted piece must be centred in the crop and match ≥ 0.7 | On the 6 affected videos + 2 that gained: 0 wrong trusted (loose: 6 games), matched games 21 → 25. Full library: matched 119 → 136, every move right 80 → 91 games, but 3 games with 5 wrong trusted plies under the old trust rule - see trust table | Yes (`2c11d70`) |

### 2026-10-13: first Titled Tuesday streams (3+1 blitz, 2 h, ~11 games each)

Traced with the new `tests/trace_video.py` (replays cached readings through
reconstruct with a trace, shows each game against its real game) and
`tests/dump_readings.py`. What broke, in order of damage:

| Finding | Fix | Result |
|---|---|---|
| **The board moves between games** on a stream (NFUuaZsR2S8: 12 px up, 4 px left after round 1). With the old box every square matched at 0.1-0.5; the off-grid retry rescued most but not all (a bishop on f1 read empty all game), so games died after a few plies | `recognize.retune_at_start`: at every opening position, re-fit the box and keep it if it reads the start better. Once per game, validated - unlike the confidence-triggered tracking that thrashed | (measuring) |
| **Premove display**: chess.com draws a premoved piece on its destination with the origin empty while the opponent thinks. 5SX0OAlLmk8: White premoved Rxd1 for 23 s; "5...Qxd1+ 6.Rxd1" went into the record (a 2-ply fuzzy bridge) and the real 5...Qa5 could never be explained | `reconstruct._premove_display`: current position + one piece of the side *not* to move displaced = premove; wait it out, no miss counted | (measuring) |
| A move shown then taken back (cancelled premove / dragged back): 38.c4 shown 3 s, board reverted, 38.Bg6 played; c4 stayed (the revert looked like move-list browsing) | undo check: after a revert to the previous ply, a different single move from there replaces the last ply | rYTURNjPO50-1 right plies 74 → 88 |
| A frame caught mid-move (piece drawn on both squares) matched *exactly* by a spurious 2-ply bridge, beating the true single move one square off | lookahead: a bridge of 2+ plies is dropped when the *next* reading is reached from the current position in fewer plies | O2XrdomtLUI-2 18 → 66 right plies, rYTURNjPO50-2 55 → 57, no losses on 12 games |
| Shortest path isn't the played one: Kf6 Ne4+ Ke5 read as Ke5 Ne4 (transposition) because the Kf6 frame was never drawn (king lifted while the knight moved) | `_stepwise` walked the skipped readings one ply at a time; could not help here (no Kf6 frame exists) and gained nothing elsewhere | **Removed** |
| Reconstruction gets very slow on a video whose readings are persistently a square off: each unexplained reading burns the full 100,000-node budget | budget tapers after 2 and 5 unexplained readings in a row | NFU 1 h+ → 2 min |
| **Lifted king**: a player drags the king (a premove) for seconds while the opponent moves; frames show no king, `_reachable` rejects them all, game lost | `_with_lifted_king`: a missing king is assumed still on its square | 5SX0OAlLmk8 game 1: 21 → 41 plies (all right) |
| Transposition read wrong (Bh6 Qf6 Bg5 recorded as Bg5 Qf6; g7-f6-e5 as Ke5): the shortest exact path wins | `_pick_path`: when exact explanations exist at the shallowest depth and one deeper, the readings skipped since the last accepted one (those ≥ 3 squares on from the current position, to ignore animation frames) vote, scoring every position along each route; needs a clear margin and a close fit | Fixed the synthetic case; on the real ones the intermediate position was never drawn (bishop/king in hand), so no change. A first version without the animation filter and without scoring the final position chose "g3 h5 g4" for g4 h5 - fixed |
| A burst of premoves reaches a position by routes nobody saw (3.Nd2 dxe4 4.Nxe4 or 3.Nc3 ...): "ambiguous", game lost after 4 plies | Guess the route when there are ≤ 3 routes of ≤ 3 plies (`GUESS_MAX_*`), record the plies as `ambiguous_plies` (confidence 0.5, trust ends before them, benchmark counts them right if the position agrees at the end of the run). Guessing bigger tangles too produced 5-ply rook shuffles | NFUuaZsR2S8 game 2: 4 → 50 plies (49 right); 5SX0OAlLmk8 game 4: 4 → 26 |
| **Flicker vote window ±2 frames → ±1** (`recognize.vote_stream`): at 1 fps a 5-frame majority merged consecutive positions when moves came every second or two (a move and the premove drawn on top of it were voted into one hybrid frame) | Same code, radius 2 vs 1 on three videos' fresh readings: O2XrdomtLUI 4740 50/73 → 106/106, rYTURNjPO50 game 2 57 → 106 right, one new match; but brief blips now get through (a piece dragged over a square for 2 s) - handled by the two undo rules below | Yes |
| A premove drawn **on top of** a real move (4.e3 with Nbd2 premoved on the same frame): `_without_premove` puts each candidate premoved piece back and the rest must be *one* exact ply by the premover; when two readings fit ("e3 + Nbd2 premoved" / "Nbd2 + e3 premoved") the next reading decides, else the plies are recorded as ambiguous | ahAoIZVTtA4 game 2 now follows the game; allowing up to 3 real plies under the premove let a noisy frame of "Nxc5+ Kb6" be read as "Nxd4 cxd4 with b6 premoved" (O2XrdomtLUI game 1 99 → 66 right) - restricted to one ply: 115/115 | Yes |
| A ply that reverts within 6 s of appearing is taken back outright (`UNDO_WINDOW_S`); a ply followed within 6 s by a reading only a *different* single move from the previous position explains exactly is a piece dragged through a square | rYTURNjPO50 game 1 back from 50/55 to 88/94 under the ±1 vote | Yes |
| Premove bursts (four plies in two seconds, seen only as the result): the search returns just the first move all routes share, then the rest is "ambiguous" | The same reading is re-examined from the new position (`redo`), and an ambiguity whose routes play the same moves in different orders is guessed too (`_same_moves`) | NFUuaZsR2S8 game 4: 5 → 29 plies all right, game 6: 5 → 20 |
| A lifted bishop read as "Bxh6 gxh6" (capture and recapture explain a vanished piece exactly), and the lookahead couldn't object because the next reading had several short routes ("ambiguous") | The lookahead counts any exact shorter route among the alternatives | 1AdC-UGksN8 game 7: 13 → 39 plies all right |
| Lookahead extended to (a) a reading explained only by the first move several deeper routes share, and (b) a single move accepted a square off (a frame caught mid-capture: "Bb5" for the trade Qb5 Qxb5 Bxb5) when the next reading follows exactly from the current position but not from after that move | shAZCVixelw game 4: 29 → 68 right, game 12: 26 → 110; A/B on three videos: 1263 62 → 106 right, 2854 63/78 → 80/80, rYTURNjPO50 game 1 88/94 → 103/103, nothing worse; NFUuaZsR2S8 2 → 6 games every move right | Yes |
| An exact explanation two or more plies deeper than a near-exact one wins ("exact at any depth beats fuzzy"): a frame caught mid-capture (the capturing pawn not yet drawn) was explained by "Ne7 cxd5 Nxd5 Bg2 Nf6" instead of Nf6 Bg2 dxc4 | The near-exact route wins when the exact one is ≥ 2 plies deeper; near-exact routes that transpose (Nf6 Bg2 dxc4 / dxc4 Bg2 Nf6) are guessed and recorded ambiguous | shAZCVixelw game 11: 5 → 56 plies (41 right) |
| A piece in hand plus the opponent's premove drawn gave an *exact* two-ply match ("Bf1 Bxf1" for Ba6 with Bf1 premoved), so neither the fuzzy nor the fewer-plies lookahead objected | The "follows exactly from here but not from after" lookahead now applies to exact bunches too (and to fuzzy bunches, not just single moves). The old cancelled-premove test now recovers the real move without clock evidence | ahAoIZVTtA4 game 12: 45 → 127 right (whole game) |
| A piece in a corner square the box clips (a knight on a8, box 4 px off) read empty all game: the "no blob" gate (`_MIN_PIECE_FRAC`) fired before the off-grid retry, which would have matched it at 0.97 | `_classify_cell`: a blob-less square far from the empty template (`_CLIPPED_DIFF`) gets the shifted retry before being called empty | 1AdC-UGksN8 game 5: 59 plies with an error at 38 → 68 all right; rYTURNjPO50 and V-udSNa70pE unchanged (no phantom pieces) | Yes |
| A whole bunch accepted from one reading that reverts within 6 s is taken back (`BUNCH_UNDO`): a rook dragged for two seconds read as "Ra3 a4 Ra1 a3", and the revert looked like browsing | A/B on 8 videos' readings: FjqGoNDp7fs 1607 50 → 115 right, qW-mT-FbLnA 950 37 → 85, shAZCVixelw 1030 72 → 105; nothing worse; 0 wrong trusted | Yes |
| The dense re-read (refine.py) clears fuzzy/ambiguous marks on a bunch it splits (every position seen in order, each exactly at least once) | 4 videos: games listed 18 → 21 of 37, trusted plies 681 → 753, 0 wrong | Yes |
| Re-fit the box only when the opening reads ≥ 4 squares off (`scan.RETUNE_MIN_ERR`) | Matching the library rescan to the old catalog *by start time* (not game ID, which shifts when a new game is found earlier): 27 games better, 91 same, 18 worse, and 13 of the 18 worse were in videos where the re-fit fired. Re5ZPpEMPHk with the re-fit on: 15/20 and 18/20 right; off: 76/76 and 43/43 - it fired on a start read 1-2 squares off (a highlight) and moved a good box to a worse one. Gated: Re5ZPpEMPHk 76/76, 43/43; the stream that needed it (read 12 off) still re-fits, 70/70 | Yes |
| `retune_at_start` scored by `autotune_box`'s alignment cost | On a real start frame the cost preferred a box reading 14 pieces (18 squares off) over one reading 30; the 2 px re-fits it produced left two of a stream's games unreadable | **Replaced**: coarse (±8 px, ±8 size) then fine grid scored by how well the known opening reads; a shape gate (back ranks full, middle empty) instead of a piece count, since the misaligned box reads only 14 pieces. ~45 s once per game start that reads imperfectly, memoised a minute after a failure |

Also found: John Bartholomew is chess.com `Fins0905` (not `Fins`), Simon
Williams `Ginger_GM`. His video ZzwYoNjK6_Q: 8/8 games matched, 6 every
move right, but **ratings 0/8** (small "(2533)" font; reads like 268, 3274)
and clocks 4/8 - open.

Whose move goes wrong first (174 matched games, live library + six blitz
streams): the narrator's in 37 games, the opponent's in 22, with about the
same number of plies each before it - the narrator's own premoves and drags
are what the board draws, so their plies are ~1.7× as error-prone.

## Trusted moves (`trust.py`)

Rule: stop two plies before a bunch of 3+ plies (or any bunch past ply 26),
and hold back the last 2 plies (4 past ply 26) unless a clock-verified mate.
Tuned on the 136 benchmarked games, so expect a small error rate on new ones,
and recalibrate as the library grows (it needed it once already).

| Idea | Result | Kept? |
|---|---|---|
| Stop at the first bunch of 2+ | 2/119 wrong games but median only 13 trusted plies | No |
| Hold back fewer end plies when both clocks were read | +17 full openings but 5 wrong last plies came back | **No** |
| Stop where the mover's clock *rose* | Fires on increment games (600+8) too; cost 6 openings, caught 1 error | **No** |
| Flag moves after which the mover's clock kept running | Flagged 19 all-correct games, caught 13 of 38 errors | **No** |
| Recalibrate on 136 games (after recognition fixes found more): trust 2-ply bunches only before ply 26 (was 30), cut 2 plies before a trigger bunch (was 1) | Old rule left 3 games wrong (two wrong early pairs, one misread 2 plies before a bunch); new: 0 wrong of 4,630 trusted plies, full openings 73/136 | Yes |
| Recalibrate on 209 games (library rescanned with the blitz fixes + 8 Titled Tuesday streams, 2026-10-14). Per ply: exact singles early 0 wrong of 1,100+, exact pairs early 288/1, 3-bunches early 76/5 (+3 at the tail), late pairs 439/12, late 3-bunches 96/4; **fuzzy** bunches (accepted a square off) early 56 right/5 wrong, late 78/16; ambiguous (guessed routes) 55/0 early, 28/2 late | Keep BIG_BUNCH 3; fuzzy and ambiguous bunches end trust (dropping the fuzzy rule would add 23-29 openings but leak 16 plies in one game, and the per-run error rate says more would follow); LATE_PLY 26 → 34: 0 wrong of 5,329, openings 71 → 87 of 209 | Yes |
| Confirm a fuzzy bunch when its resulting position is then read *exactly* for 2+ s (`reconstruct.FUZZY_CONFIRM_S`). On the 209 games, all 7 wrong early fuzzy bunches were never read exactly before the next move; 24 right ones were | A/B on 41 library videos (132 matched games), moves identical: 0 wrong trusted both arms, trusted plies 3,345 → 3,567, games shown (12+ trusted plies) 95 → 100, whole openings 51 → 59 | Yes (2.0) |
| A missing king (`reconstruct.HIDDEN_KING`): try castling as the explanation, and mark the plies of the king's side as guesses (they end trust). Aig9-ZyQ8Ak (black king unread in 45% of frames): the lifted-king repair kept the king on e8, so ...O-O read as an exact ...Rf8 and 4 wrong plies were trusted | A/B on all 76 live videos (reconstruct only): wrong trusted games 1 → 0, matched 337 → 338, every move right 199 → 200, right plies +61; trusted plies 8,002 → 7,880, shown 225 → 220 (trust lost where kings go unread - see the template re-cut row under Recognition) | Yes |
| What precedes a wrong move? Per accepted ply, the events since the previous one, over 194 videos (~100,000 scored plies, reconstruct only): nothing unusual 70,916 right / 196 first-wrong (0.3%); unexplainable reading a square off 24,534 / 287 (1.2%); undo 366 / 13 (3.4%); browsing back 118 / 8 (6%); detour retract 13 / 7 (35%). (scratch: handoff/scripts/context_stats.py) | Basis for the two rows below | - |
| Moves accepted right after an undo, browsing back or a detour end trust (`reconstruct.RETRACT_ENDS_TRUST`, `doubtful_plies`) | A/B on 194 videos: wrong trusted games 9 → 6, but trusted plies 39,321 → 37,724, shown 1,064 → 1,048, whole openings 727 → 682. Undo is the commonest and least risky of the three - try without it, and let refine clear the mark | Off (committed, measuring) |
| ...plus a bunch (2+ plies) accepted right after an unexplainable reading a square off (`reconstruct.UNREACH_BUNCH`) - 5 of the 6 remaining leaks | Replayed (`tests/trust_replay.py`, 1,620 games): wrong trusted games 9 → 2, but whole openings 729 → 328, shown 1,068 → 725. Ending trust at *any* bunch after one: 9 → 0, openings 729 → 10 | **No** (off) |
| Replay trust rules from a once-built log (`tests/trust_replay.py`): a doubtful-ply rule doesn't change which moves are picked, so rebuild once with what happened before each bunch, then score rules offline | 9 rule sets in 0.8 s vs ~2 h per A/B. Checked against the real RETRACT A/B: same 9 → 6 and same three games fixed; cost over-counted (openings −66 vs −45) because the undo path that replays a corrected move wrote no `ply` trace note (added) | Yes (tool) |
| Retract variants (replayed): browse+detour only; detour only | browse+detour: 9 → 7 for −30 openings; detour: 9 → 8 for 0 | No (too little) |
| "Messy stretch": end trust once m events (unexplainable reading a square off, undo, browse, detour, fuzzy-ambiguous) land within W plies (grid W 4-20, m 2-4) | Best: 9 → 1 for −533 of 729 openings. These events are everywhere in streams | **No** |
| Back off where trust already ends: to the earliest bunch after an unexplainable reading a square off within the 5 plies before the cut (`trust.ROUGH_BACKOFF`, `rough_plies`). Every leak's first wrong move was 1-5 plies before the cut | Replayed on 1,620 games: wrong trusted games **9 → 0**; shown 1,068 → 1,020 (−4.5%), whole openings 729 → 637 (−12.6%), trusted plies −8.7%. Window 4: 9 → 1 for −30 shown, −53 openings. Undo/browse/detour in the event set: no difference. Uniform back-off of 5 plies: 0 leaks for −188 openings. Tuned on 9 leaks: to be confirmed by the full rebuild | Yes (5) |
| ...checked on the rebuild (168 of 189 videos done, 1,383 matched games) | 3 wrong trusted games left (QlJs51SMKg4 ×2, XNNNC0b-G8I), all of them among the replay's "fixed" 9. The replay log was built without refine.py: these games had a late 2-ply bunch that ended trust before the error, but refine splits it and trust runs on. All three are undo/detour leaks: the first wrong move comes right after an undo or a detour. New `trust_replay.py replay --marks CATALOG` takes times and marks from a scanned catalog, which reproduces exactly these 3 | Back-off kept; leaks handled by the row below |
| Undo or detour ends trust, browsing back doesn't (`reconstruct.RETRACT_ENDS_TRUST` without browse). Replayed with `--marks` on the rebuild (1,349 games) | Wrong trusted games **3 → 0**; shown 954 → 941 (−1.4%), openings 654 → 611 (−6.6%), trusted plies −5.0%. Full retract (with browse): 3 → 0 for −38 shown, −63 openings. Browse+detour: 1 left. Undo or detour only within 6 plies after a rough ply: 0 for −12 shown, −41 openings (no real saving) | Yes (on; merged 2026-10-20) |
| The 2026-10-20 merge: the full rebuild (194 videos) with the rough back-off, undo/detour marks from the replay logs and drop_final_bunch | 1,653 matched games: wrong trusted games 10 → **0** (0 of 44,588 trusted plies); clocks 63.9% → 82.5%; ratings 74.2% → 76.9%; every move right 72.7% → 72.8%; listed 1,227 → 1,187 (−3.3%), openings 886 → 736 (−17%), trusted plies −11%. The replay log only kept matched games, so the 116 unmatched games first missed the undo/detour marks (`build --keep-unmatched` now) | Yes (live) |
| Drop a game's final bunch (2+ plies at one timestamp, `catalog.drop_final_bunch`): a quarter of games with a wrong move were wrong only in the last 1-2 plies, mostly such a bunch (the narrator showing a line after the game, or the video moving on). Those plies were already untrusted | merge1 candidate (1,653 matched): every move right 66.4% → **72.7%** (chess.com 62.7 → 69.6, lichess 74.3 → 79.5); shown 1,227 → 1,227, openings 887 → 886, trusted plies −35 (−0.07%). Dropping *any* final bunch: 72% in replay but 1,253 right plies lost | Yes (applied at the 2026-10-20 merge, then on by default) |
| Clock OCR in-process (tesserocr) instead of pytesseract's process per call (`clock_read._tesseract_line`, `TESSERACT_INPROC`) | ~25 ms vs ~104 ms a call under load; clock tests 4:03 → 2:04, all 53 pass both ways; 397/400 random crops from 5 channels give identical text (3 differ only in spaces inside junk), parsed clock values 0/400 differ | Yes |
| Cache the hi-res rating frames losslessly (`download.fetch_hires_frame`, `scanner/cache/hires/`) | 10.3 s fetch → 0.03 s from disk, identical pixels. A rescan or ratings rescore no longer needs the network | Yes |
| Clock OCR last fallback: read only the tallest text line of the box (`clock_read._tallest_line`, `CLOCK_TALLEST_LINE`). A motion-found lichess box can take in the shrinking time bar and the name row (Jqz9fdHOrPQ: 152x81 around a 37-px clock) | Jqz9: the 8 games whose clocks regressed with the current box choice all fixed (e.g. -17 31/42 → 42/42, -32 110/122 → 122/122), 4 improved further (-75 79/97 → 105/106); clocks OK 8 → 12 of 12. chess.com guard (24 games, 4 channels): 20 → 23, no regressions | Yes |

### 2026-10-21: where "every move right" fails (449 of 1,653 matched games on the live catalog)

First error of each failing game, against the real game (scratch script: first error, what kind, whether the record lines up again):

| Kind | Games |
|---|---|
| Diverges and never lines up again, **within 6 plies of the record's end** (the real game goes on 10+ plies in 151 of these: a wrong ply derails reconstruction and the game closes after 10 misses) | 178 |
| Diverges, mid-game | 141 |
| Plies missing, then lines up again (44 of them: exactly 4 real plies missing, a repetition such as Ng4+ Kh7 Nf6+ Kh6) | 62 |
| Same moves in a different order (transpositions inside 1 fps bunches) | 37 |
| Extra plies, then lines up again | 30 |

Marks at the first wrong ply: none 166, rough 119, fuzzy 69, fuzzy+rough 55, doubtful 16, ambiguous 10 (some overlap).

| Idea | Result | Kept? |
|---|---|---|
| `_placement_of` from the bitboards instead of parsing `board_fen()` (it was ~65% of reconstruction's time) | Identical on 35,594 random positions, 3.5× faster a call; a 700-s stretch of 5Fw9tq1aUUE reconstructs in 3.7 s instead of 6.8 s | Yes |
| **Recover from a derail** (`reconstruct.RECOVER`, `_recover`): when a reading 3+ squares off can't be explained (misses ≤ 3), and the next reading doesn't follow from here either, re-explain it from up to 6 plies back: first one recent piece's two moves merged into one (traced: 21.Qe2 Rfc8 22.Qc2 for 21.Qc2 Rfc8, a tempo out, and the game closed 63 plies early), then the fewest plies taken back with a route starting differently. Exact landing required, the next reading must follow (a square off allowed); the replaced plies are doubtful (end trust) | First version, recovery at misses 3-9: never fired (the readings are 7+ plies on by then; a 500k-node search fails). At misses ≤ 3, on hard+guard (39 videos, 600 matched): every move right 414 → 417, right plies +332, but correct games broken by a frame caught mid-move (6RTjPSXYrPE 126/126 → 64). + next reading must *not* follow from here: 418, right plies +414, but 4 correct games made wrong: recoveries right after browsing/undo (a line being shown) or on an "ambiguous" reading (post-game analysis). + no recovery within 30 s of an undo/browse/detour (`RECOVER_QUIET_S`), nor on ambiguous readings: **421** right games (+7), right plies +332, 0 wrong trusted both arms, trusted plies −12, shown −1, matched −1; no correct game made wrong (PEpvyA_2kG4 65/65 → 61/61). Allowing ambiguous readings (RECOVER=2): 420. **Whole library** (193 videos, 1,654 matched, reconstruct only): every move right 1,128 -> 1,136, right plies 103,205 -> 104,202 (+1.0%), 0 wrong trusted games both arms, trusted plies -17, shown -1; 30 games better, 16 worse; 10 all-right games get an error (4 of them still gain right plies, e.g. L4vInFpEv4k 53 -> 90 right; worst uuDQbMlXeDQ 48 -> 25) | **Yes** (on by default) |
| ...plus never recover to a shorter line, nor from a reading needing the lifted-king repair (`RECOVER_STRICT`) | On the 60 videos recovery changed: every move right 417 -> 415, right plies -257, trusted +21. Fixes the games it was aimed at (uuDQbMlXeDQ 25 -> 48) but blocks good recoveries: a derail can rightly replace plies with fewer (five misread plies by three real ones, 9lh35S3kO10 21 -> 134 plies followed; a spurious exchange "Qe7 Bxe7" by the real Be7) | **No** (off) |
| **Drop unsettled final plies** (`catalog.drop_unsettled_tail`, `reconstruct.Game.settled`): a ply is settled when its position is read exactly again before the next ply (or within 60 s of the last). A game's final plies that never settled are where it derailed; drop up to 10 of them, keeping 10+ plies. Offline on 1,656 matched games (reconstruct only, no final-bunch drop): every move right 61.0% -> 72.2% (only games closed by "too many misses", cap 6), 75.7% (any close, cap 6), 78.7% (cap 10, -1.1% right plies), 81.9% (no cap, -1.4%); 56% of all plies settle | Whole library through the catalog (final-bunch drop on, recovery on): every move right **68.6% -> 80.3%** (1,136 -> 1,327 games), 0 wrong trusted both arms, right plies -1.1% (all untrusted), trusted plies 35,421 -> 35,277, shown 1,028 -> 1,024, matched 1,655 -> 1,652 | **Yes** (cap 10, `CATALOG_DROP_UNSETTLED`) |
| Brief one-move undos (a piece dragged to show an idea: Naroditsky's habit while explaining) not ending trust. 51 of 89 hidden Naroditsky speedrun games were hidden by "doubtful" plies after such undos (other channels: 17 of 554). Replayed on the whole catalog (1,893 games, `trust_replay.py` with finer undo kinds) | Never ending trust: shown 1,221 -> 1,255 (Naroditsky +23), openings +72, but **1 wrong trusted game** (QlJs51SMKg4 7312: two readings a square off, then the undo, then the first wrong move). Ending trust only when a reading was a square off within 1 / 3 / 5 plies before: 0 wrong in all; shown +16 / +10 / +8 (Naroditsky +11 / +6 / +6), openings +28 / +17 / +7 | Yes, 3 plies (`reconstruct.BRIEF_UNDO_NEAR`), applied 2026-10-23 to the catalog after all Naroditsky batches (doubtful plies recomputed from cached readings, rule off vs on; moves unchanged): wrong trusted games 0 -> 0, listed 1,416 -> 1,436 (Naroditsky 290 -> 308), trusted plies 51,057 -> 52,126, whole openings 821 -> 845 |
| A single move played after the game ended (Naroditsky batch 4: the opponent resigned, he then played the mate on the board, and it was trusted; `catalog.drop_final_bunch` only drops 2+ plies at once) | Measured on the 1,986 matched games after all Naroditsky batches: only 2 games are right to the real end and then carry extra plies (1 and 4), and trust stops before the real end in both. The batch 4 game was trimmed by hand | No rule: one case in ~2,000 games gives nothing to tune a rule on; the merge gate (0 wrong trusted) catches the next one |
| `recognize.looks_like_start` pre-filter: up to 16 occupied-looking middle squares instead of 10. Naroditsky's full-height blue chess.com board at 720p reads 11-13 empty squares as occupied (CLAHE stretches the flat squares' compression noise), so three Sensei speedruns never calibrated although `calibrate_from_start` accepted every start frame (traced on S-PGKHRQM5o: start shown 4.2-6.0 s, all calibrate) | Rescanned the three: two now give games (qhHtJcXkkfg 1 game, 53 trusted plies, right; TvgWwc1dBb8 1 game, right), S-PGKHRQM5o calibrates but reads 630 of 1,598 frames and finds no game (the tuned box, 568,10 706, sits inside the real 562,0 718 board). Catalog videos already calibrated are unaffected unless rescanned; calibration's own gate (6 squares) is unchanged | Yes |
| Try the video's other boards: `board_detect.detect(others=...)` returns the other clusters with 2+ votes, and `scan.py` reads the frames again with each (up to 2) when the most-voted board never showed an opening position. Two Sensei speedruns show the online games on the right, then his old tournament games in a database on the left for longer, which won the vote | iuLI_EUlgvE: 2 games (matched, trust 11 and 0, both right where trusted); GbY7R4GYN_M: 1 game (right, trust 8). Costs a second pass over the frames only for videos that would otherwise give no games. All 5 recovered games matched, 0 wrong trusted | Yes |
| Hidden 4-ply repetitions: the flicker vote smooths away a repetition played a second a move, so the record loses 4 plies. Raw frames show the cycle in 31 of the 44 games, its first position (which browsing the move list never shows) in 23. Detector: quiet move, reply, both reversed, accepted when raw frames since the last move show the first cycle position and one more, each a square off at most, and only one cycle fits | On the 44: 8 right, 3 wrong, 33 not found. On gaps of 150 all-right games: fired in 5 (false). At 1 fps the frames are too few and too noisy; a dense re-read of such moments might work | **No** |
| **Fit the box by the board's grid** (`recognize.TUNE_GRID`, `autotune_box`/`_grid_cost`): fit the box by the light and dark squares of the empty middle ranks instead of by the pieces, for streams where the box found on the pieces sits a few pixels inside the real board | On 78 Danya videos that gave no games: 100 games, 75 matched, 58 every move right, 45 listed, 1,706 trusted plies, 2 games with a wrong trusted move (one a benchmark false match, now in `fixtures/false_matches.json`; one a frame caught mid-step while browsing back, fixed by the row below). On 22 catalog videos, always on vs off: 251 vs 272 games, 236 vs 262 matched, 191 vs 218 every move right, 149 vs 171 listed, 5,805 vs 6,874 trusted plies, 0 wrong both. It helps some videos (Aig9-ZyQ8Ak, zA4K0Em0h1I) and badly hurts others (Jqz9fdHOrPQ 53 -> 35 listed) | **As a fallback only**: `scan.py` re-reads with it when the ordinary read would keep no games (`_any_kept`); the readings cache records `tune_grid` so `--from-readings` fits the box the same way |
| **A ply that moved the board toward the position the viewer then browsed back to** is taken back (`reconstruct.BROWSE_STEP_S` 3 s, `BROWSE_STEP_PLIES` 2): a frame caught between two positions while stepping back through the move list reads as a legal move (traced: 9.Qe4 Nxf4, then browsing back to before 9.Qe4, read as "10.Qa4" a second earlier, and trusted) | Changes 29 of 584 videos with cached readings. Fz0_pECE5vo game 1: 23 plies with a wrong trusted one -> 38 plies, all right. Up to 2 plies vs 1 on the 14 videos where it matters: 8,438 vs 8,402 right plies, 128 matched games each | Yes |

### 2026-10-24: where games stop early (completeness)

On the catalog before the 2026-10-24 merge (1,991 matched games), 1,544 have every move right, but only 402-424 are read to the end of the real game. The games with every move right hold 73% of the real plies, and 31% of real plies are shown.

| Finding | Detail |
|---|---|
| The missing tails are fast | In right-but-incomplete games, 36.6% of the missing plies were played in under 1 s, against 16.5% of the plies we read. In 570 of 1,142 such games the mover still had 60 s or more when the record stopped |
| How much is missing | 419 games miss 1-4 plies; 306 miss more than 40 |
| Why reconstruction closed the game (183 traced) | Too many misses 140, gap 32, new start 9, mate 2. In 93 the record was right up to the close (stuck); in 90 a wrong ply came first. 152 lost plies to the unsettled-tail drop |
| Stale scans | 17 of the 183 games gain 10+ right plies with current code (491 plies in all, e.g. Wl1gISrGdxw): rebuilding from cached readings is worth doing after reconstruction changes |
| **The flicker vote blends positions** | `recognize.vote_stream` takes a 3-frame majority per square. When moves come every second it can produce a position that never existed (traced on XNNNC0b-G8I around 4,500 s: a king walking f1-e2-d3 voted off e2), while each raw frame shows its position exactly. `refine.py` re-reads bunches at 6 fps, but only to re-time them, never to change moves |

| Idea | Result | Kept? |
|---|---|---|
| **Try the raw frame** (`reconstruct.RAW_FALLBACK`): a voted reading nothing explains is tried again as the frame read before the vote; repeats whose raw frame changed are kept. A raw frame may add moves, never take any back (one showing an earlier position is the viewer browsing: traced, 24...Qe5 undone for "Qe7"), and its moves are dropped when the next reading follows more directly from before them (a piece caught on its way: 7.Bc4 read as "Bd3"; 19...Rxe3 read as "Re5", then "Re2 Rxe3 Rxe3") | First whole-library run (584 videos, reconstruct only, without the last two checks): matched 2,104 -> 2,147, read to the end 688 -> 772, right plies +6.6%, trusted plies 44,205 -> 50,917, but **2 wrong trusted games**: the raw frame split a 3-ply bunch the vote had made, and that bunch was what ended trust before a later misread | Not as it stood |
| ...ending trust on every raw ply whose voted reading was a square off (`RAW_DOUBTFUL` 1) | 0 wrong, but trusted plies 50,917 -> 36,146 (below the 44,205 without the fallback), shown 1,452 -> 1,130 | **No** |
| ...ending trust only where a raw ply and the next accepted plies, within 5 s, would have made a 3+ ply bunch (`RAW_DOUBTFUL` 3, `RAW_SPLIT_S`; 1.5 s left nq4E2SLMyJY wrong: the raw ply came 3 s before the other two) | Whole library vs no fallback: matched 2,104 -> 2,149, every move right 1,685 -> 1,695, read to the end 688 -> **771** (+12%), right plies 122,538 -> 130,289 (+6.3%), trusted plies 44,205 -> 46,550 (+5.3%), shown 1,303 -> 1,333, openings 727 -> 749, **0 wrong trusted both arms**. Losses: right plies down in 80 games (-1,629; worst sJ649Mg0N4g 1537 104 -> 46, games that now end early) against up in 307 (+6,768); 89 all-right games pick up an error, 70 go the other way; trusted plies down in 120 games (-1,787; 55gXMP9N5sM 83 -> 9) against up in 296 (+3,958); 29 matched games lost (1,060 right plies), 74 new (3,672) | **Yes** (on by default) |
| ...when another route from before a raw frame's moves reaches the next reading in as many plies (a tie, which the check above lets through): 1 = end trust at them, 2 = take that route (`RAW_TIE`). Prompted by the full rebuild's one wrong trusted game: _ZAodluTDL4 6241, 60...f5 61.gxf6 e.p. read as "60...f6 61.gxf6", the pawn caught on f6 on its way, the same position after, 85 plies trusted with clocks | Whole library, reconstruct only: 1 = trusted plies 46,550 -> 46,390, shown 1,333 -> 1,331, nothing else changed; 2 = right plies 130,289 -> 128,758, read to the end 771 -> 756. Neither fires on the traced game: the reading after the raw frame was 3 squares off, so there is no exact next position to route to | **No** |
| ...ending trust at a raw frame's moves when the next distinct reading is a miss or a premove instead of following from them (`RAW_CONFIRM` 1). On the traced game the readings after "60...f6" were misses, then the pawn on f5 taken for a premove | Fixes the traced game (trust ends at ply 59). Whole library, reconstruct only: moves unchanged, but trusted plies 46,550 -> 44,778 (-3.8%, 115 games lower: iFdoGDFnT3c 2296 72 -> 10), shown 1,333 -> 1,297, openings 749 -> 705: most of what the raw fallback gained | **No**: too broad |
| ...ending trust there only when the moved piece going to another square from the same one fits that next reading better (`RAW_CONFIRM` 2: 60...f5 instead of "f6" takes the reading from 3 squares off to 1) | Fixes the traced game (trust ends at ply 59). Whole library, reconstruct only: moves unchanged, trusted plies 46,550 -> 46,511, shown 1,333 -> 1,332, openings 749 -> 748, all from one game (aHzb3XGiG08 1781, 43 -> 4, right but caught). The 46 videos it changes, rescanned with clocks and put in the rebuild: 0 wrong trusted of 2,115 | **Yes** (on by default) |
| ...when nothing follows from a raw frame's moves, let any route from before them win (traced: 25...Rxd1 26.Rxd1, the rook caught on d2, read as "25...Rd2+", and sJ649Mg0N4g 1537 closed 58 plies early) | On the 16 videos where the raw fallback lost the most right plies: with routes a square off allowed, right plies 9,823 -> 9,638 and read to the end 44 -> 42 (fixes sJ649Mg0N4g, but a route a square off beat a right raw move: 15...Ba6 lost to "Na6", 29.Bc4 to "Ba6 Bxa6"); exact routes only, 9,757 and 43 | **No** |
| **Re-read stalls at 6 fps** (`refine.reread_stalls`, STALL_FPS 6, 4 s before to 25 s after, 2 rounds): where reconstruction closes a game on "too many misses", read that window again densely and replace the 1 fps readings | Hard+guard (39 videos): matched 603 -> 647, read to the end 185 -> 218, right plies 39,131 -> 44,679 (+14%), trusted plies 12,111 -> 12,977, listed 354 -> 379, 0 wrong trusted both arms. Losses: every move right 487 -> 482; 17 games worse (2-8 plies each), trust lower in 4. Cost: 466 stalls, ~9.5 worker-minutes a video | Promising; see the next two rows |
| ...on top of the raw fallback (both arms with `RAW_FALLBACK`) | Hard+guard (39 videos): matched 626 -> 656, read to the end 217 -> 237, right plies 43,788 -> 46,907 (+7.1%), trusted plies 13,092 -> 13,605, but **1 wrong trusted game**: OK4pBfkM-6w 415, 20 of 36 plies right with 34 trusted. The window after a game closes is where the narrator steps back through it, 6-7 plies a second; at 1 fps that never formed a game, at 6 fps it does. 11 such second copies of an earlier game appeared (none in the 1 fps arm). Cost: 368 stalls, 705 worker-minutes (18 a video) | Not as it stood |
| ...dropping a game that opens with 10+ plies of an earlier game in the video **and** has 14+ plies within 4 s (`reconstruct.REPLAY_SHARED`, `REPLAY_FAST`). Neither alone separates them: real games in one video share up to 20 opening plies, and at 6 fps a premoved opening reaches 18 plies in 4 s; real games sharing 10+ plies peaked at 11 in 4 s | Applied to the same arms: drops 8 of the 11 copies (the other 3 are slow and short), including the wrong one; drops nothing among the 2,422 live games or the 1 fps arm, nor at 8 and 12. Matched 626 -> 648, read to the end 217 -> 237 (+9.2%), right plies 43,788 -> 46,588 (+6.4%), trusted plies 13,092 -> 13,420 (+2.5%), shown 373 -> 381, **0 wrong trusted both arms**. Losses: every move right 491 -> 483 (mostly games now read further that pick up a later error: 37/37 -> 64/89); right plies down in 20 games (-380) against up in 121 (+3,499); trusted lower in 8 games, higher in 33; 6 matched games lost. **Held out** (48 random other videos, guard in both arms): matched 151 -> 157, read to the end 57 -> 61 (+7%), right plies 8,147 -> 8,557 (+5.0%), trusted plies 3,068 -> 3,129 (+2.0%), shown 96 -> 99, **0 wrong trusted both arms**. Losses: every move right 120 -> 116 (10 games pick up an error, 9 of them in plies they hadn't read before: 29/29 -> 113/168; OhBPJlQYnvc 893 118 -> 113 is the one real loss); right plies down in 7 games (-127) against up in 27 (+537); trusted lower in 2, higher in 10; 3 matched games lost, 9 new. Cost: 155 stalls, 9.3 worker-minutes a video (18 on the hard ones) | **Yes** (in `scan.py` by default, `--no-stall-reread`; re-read frames cached in `VID-stalls.json` so a `--from-readings` rebuild reads them once) |
| **Missed games**: place each video in wall-clock time from its matched games' end times, then count the archive games inside that span that aren't in the catalog | Unedited videos: 198 of 879 real games missing (22.5%); edited videos: 371 of 1,369 (27.1%); 281 videos can't be placed. Of the 198, 107 overlap an unmatched catalog game (found but too short to match) and 91 were never found. Cause traced in the "never found" ones: the board moves or changes size mid-stream (shAZCVixelw: 646 -> 610 px at ~6,343 s, nothing read after), and the box is re-fitted only at opening positions | Measurement |
| Re-detect a lost board: after the first game, 120 frames in a row with under 10% readable means the board is lost; detect it again from the next 20 minutes and, if the box changed, read the rest with it (templates re-cut at the next opening) | shAZCVixelw: matched 8 -> 10, right plies 479 -> 657, 0 wrong, 650 s to re-read. On 256 videos: lost in 255, but the box was unchanged in 161 (the board was covered or the stream showed something else) and no board was found in 6; 88 re-read, 54 of them with no opening position after (the new box was another screen). Matched 996 -> 1,013, every move right 792 -> 805, read to the end 298 -> 304, right plies 57,350 -> 58,599 (+2.2%), trusted 20,807 -> 20,867, shown 615 -> 616, 0 wrong both arms. Losses: right plies down in 3 games (-42) against up in 16 (+452); trusted down in 4 (-93; x-TMzSM51Cw 1218 46 -> 6); 19 matched games lost (1,260 right plies) against 36 new (2,099). Cost: 843 worker-minutes for the 88 re-reads | **No** for now: too little for the cost. Re-reading only after a lost board that shows an opening position later would cut most of it |
| **Whole-library rescan with the stall re-read** (2026-10-26, `--from-readings`, 549 videos; first 315 scored, best copy of each real game against the live catalog) | Right plies 79,512 -> 85,894 (+8%), but read to the end 267 -> 243 (-9%), trusted plies 32,514 -> 31,418 (-3.4%), extra copies of a real game 12 -> 40, **1 wrong trusted game**. Even merged per video (each video keeps its old games unless the new ones are at least as good), read to the end stayed below live (249). Two causes, in the next rows | **Not merged** as it stood; rerun with the fixes below |
| ...**a game's last move lost** after a quick resignation: `clock_read.live_move_count` looked for a clock tick from 1 s after a move, and a move timed at 6 fps (130.33 s) missed the tick in the first whole second. Now any tick more than 0.5 s after | 41 games lost their final move. The fix restores it on all 5 videos traced (TaM0A-J2dQY, cUOUbdsf7LE, JU1pLlOV04Y ×2, Mre1JH64oWs); whole-library effect measured in the next rescan | **Yes** |
| ...**post-game review copies** the replay rule misses: a narrator stepping through a finished game at playing speed, sometimes into a variation (the wrong trusted game: rYTURNjPO50 879, 19 plies trusted, branching from the real game at ply 12). Drop a game whose clocks were read but never ran (each side read spans 1 s or less, 3+ reads) **and** that opens with 10+ plies of an earlier game (`reconstruct.REPLAY_FROZEN`, after clocks are read). Among the 32 games with frozen clocks, real games shared at most 8 opening plies with an earlier one | On 2,324 rescanned games: drops 4, three of them copies of an earlier real game (including the wrong one), one unmatched; no real game dropped | **Yes** |
| Keep a game's final ply when the narrator browses back soon after it (3Qw4AL8RDOw: 22.Qxc7 found by reconstruction, then trimmed by the unsettled-tail drop) | Traced at 6 fps: for the 1.2 s before the narrator browsed back, every frame read c7 as empty, never as a queen (the square covered on screen). The position was never read exactly, so the drop is doing its job | **No** (left as is) |
| **Pass 2**: the whole library again with the final-move fixes, the tenths-clock fix and the review-copy rule (2026-10-27) | Merged per video into the live catalog (115 videos kept their old games): read to the end 468 -> 507, right plies +4.4%, trusted plies 54,740 -> 55,056, listed 1,523 -> 1,551, **0 wrong of 55,056 shown**. Losses: every move right 1,726 -> 1,698 games | **Yes** (merged) |
| ...**where the tail-trimming steps cut real endings** (each step now logs what it drops; joined with the real games, matched games right up to the trim) | Clock step ("clocks never ran after"): right plies dropped in 557 games (1,305 of 1,900 dropped plies were right); in 463 of them the real game's last 1-3+ plies. Unsettled tail: 424 games (1,184 of 1,762 right). Final bunch: 118 games (243 of 657 right). 518 games would be read to the end without any trimming. Two causes in the clock step: a mate read in the same frame as the ply before it (the check for a running clock looks only since that ply, an empty window), and a quick resignation (no clock runs after the real last move) | Measurement |
| ...in the clock step, count a mate when a clock ran in the 5 s up to it (`game_end.MATE_BEFORE_S`), and keep any ply played while a clock was running in the `LIVE_BEFORE_S` s up to it: post-game analysis comes after both clocks have stopped | Whole library, warm, against neither (2,190 matched): mate 5 s: read to the end 522 -> 687, every move right -1; + live 1.5 s: 788, -5; **+ live 3 s: 867 (+66%)**, right plies +1,451, trusted +383, every move right -16 (51 wrong plies kept at the end, none trusted), 1 complete game lost; + live 5 s: 876, -28, 3 complete lost. **0 wrong trusted in every arm**; no game lost right or trusted plies. Unmatched games: +135 plies, +2 trusted | **Yes** (mate 5 s, live 3 s) |
| ...in the unsettled-tail drop, let an exact raw frame settle a ply (the 3-frame vote can blend it away; `reconstruct.SETTLE_RAW`), and, separately, an exact reading up to 30 s after the ply even past the next one (`SETTLE_AFTER_S`). Prompted by: of the dropped plies whose position a later 1 fps frame matched exactly, 460 were right and 37 wrong | Whole library, warm, against the clock-step fix: raw frames: read to the end 867 -> 877, right plies +66, trusted +26, **no game worse**, 0 wrong trusted. + 30 s after: also 877, right plies +143, every move right -5, 1 complete game lost. Most of the 460 lie outside both windows: the unsettled drop's right losses need another explanation | **Yes** (raw frames only) |
| ...let a reading that differs only in one of the ply's pieces read empty, away from the move's squares, settle it (`reconstruct.SETTLE_COVERED`): something on screen covering a square. Logged for every cut ply (time, closest reading, differing squares): one-square-off differences of that kind were right 232 : 46 wrong, while a piece read where the position has none was mostly wrong | Whole library, warm: read to the end 877 -> 884, right plies +564, trusted +126, 0 wrong trusted, but every move right -37 (the derailments it lets through carry on for several plies) | **No** |
| **Rescan templates fitted at their own box** (2026-10-28): a `--from-readings` rescan cut its piece templates at the box `calibrate_from_start` tunes, then read the 6 fps frames with the readings' box, a few pixels off in nearly every video. In 76 videos the 6 fps frames matched the 1 fps readings on under 10% of moments (M_iIF9efRgQ: half the white pieces never seen), so the stall re-read replaced good readings with garbage. Now the fit is chosen among the first 5 opening frames by how many of ~30 sampled frames it reads as saved, kept with its box, and the 6 fps frames are read with that box | Whole library (cold for the 6 fps frames, 9 h), against the same code before: matched 2,191 -> 2,220, right plies +1,944, read to the end 877 -> 903, trusted +1,214, listed 1,409 -> 1,445; **1 wrong trusted game** (BdcY0WCN29Y-4, next row). Merged per video: live read to the end 754 -> 782, 0 wrong. Losses: every move right lost in 27 games (gained in 23), read to the end lost in 13 | **Yes** |
| ...a ply from a raw frame right after a brief undo that didn't end trust ends it (`reconstruct.BRIEF_UNDO_RAW`). Traced: BdcY0WCN29Y (a blurry broadcast) 7.Qd2 read, taken back as a brief undo (the real game went on Qxd2+ Kxd2 at once), then "7.c3" from a raw frame, trusted | Whole library, warm: changes 2 games: BdcY0WCN29Y-4 (trust 13 -> 10, 0 wrong) and qv6_noaf7BA-8 (trust 25 -> 20, all right) | **Yes** |
| **Missed games, remeasured** (2026-10-28; real games of the narrator inside an unedited video's span, not in the catalog) | Before the template fix 189 of 867 (21.8%), after 162 of 850 (19.1%); "never found although the board was readable" 84 -> 60. Of those 60: 17 never show the start position in the game's orientation (traced 8C69k-ce4-c: the waiting screen showed the start from Black's side, the site then flipped the board for a game as White, and the open "game" read the new one upside down until it closed, too late to open mid-game) | Measurement |

## Arrows (from 2026-10-21)

Narrators draw arrows while they talk ("try to go here, do this, checkmate" over an arrow h2-h6),
which turns vague commentary into concrete lines. `arrows.py` reads them off one board crop;
`tests/arrow_synth.py` (narrator-like arrows drawn onto real frames: mostly the next moves played,
spread over opening, middlegame and late game) and `tests/arrow_real.py` (35 real frames, labelled
by Claude by eye, not yet checked by a person) measure it.

| Idea | Result | Kept? |
|---|---|---|
| Score every queen/knight line: shaft coverage by the brush colour, head present, colour stops past the tip; best first | First synthetic run: recall 4% (chess.com) - the head check sampled where a triangular head is still narrower than assumed. Fixed: 93% / 91% recall on random arrows | Yes |
| Realistic synthetic arrows (the game's next moves, alternatives, any lines) | chess.com 95% precision / 89% recall, lichess 80% / 78%: real arrows were already on many frames (frames showing arrows are now skipped: 3 of 63 chess.com, 8 of 68 lichess) | Yes (test) |
| lichess colour model: the brush blended over light and dark squares, piece bodies and the last-move highlight; a pixel must also stand apart from those plain colours (red over a brown wood board is close to the board) | One-square arrows found (they lie half over the moving piece); 4 false red arrows on a wood board gone | Yes |
| Thin: colour stops beside the shaft (a highlighted square is filled) | A false one-square arrow on lichess's green highlight gone | Yes |
| Past the tip the colour must carry straight on for a square to count against the arrow (arrows meeting on a square) | chess.com synthetic recall 89% -> 94% | Yes |
| Tail must be narrow (direction of translucent arrows) | Real lichess: two arrows read backwards now not read at all (precision 15/18 -> 16/17) | Yes |
| Erase only a found arrow's shaft and head before the next (it erased a head-wide band, wiping a meeting arrow's head) | Synthetic chess.com 99% -> 100% precision | Yes |
| Threshold 0.55 instead of 0.6 | Real lichess +1 arrow, synthetic +1 false arrow | No (precision first) |

**Now:** synthetic (60 frames each) chess.com 100% precision / 95% recall, lichess 100% / 96%;
real (35 frames, 35 arrows) chess.com 15/15 and 15/15, lichess 16/17 precision, 16/20 recall.
Real arrows: the reader fired on ~5% of random chess.com game frames and ~11% of lichess ones.
Open: two arrows leaving one square in opposite directions read as one long arrow; circles
(square marks) aren't read; chess.com's other brush colours not seen yet.

## Clocks

| Idea | Result | Kept? |
|---|---|---|
| Tesseract OCR instead of self-calibrating digit templates | Self-calibration was confidently wrong for whole games | Yes (`5b80713`) |
| A failed clock read is retried with the crop widened leftwards by one box width (`clock_read.WIDEN_LEFT`). chess.com's clock is right-aligned and grows leftwards under 20 s (timer icon + "0:05.0"): the box found on the normal display clipped it, every low-time value was lost and the last one (20) carried to the end of the game (ahAoIZVTtA4 game 7) | Clocks re-read on the 84 failing games + 40 passing ones: clocks OK 40 → 77 of 124, 0 regressions caused by it. Re-run with the retry off on the 39 changed games: 32 pass only with it, 6 pass either way (rescore baseline drift), 1 (3fS-8ke1eaQ-5) reads worse than live either way, better with it (7 wrong vs 77) | Yes |
| ...but not while screening candidate boxes | 3fS-8ke1eaQ-5: 7 → 77 wrong values | **No** |
| Drop a chosen clock that never stands still, then re-pick (lichess arena's tournament timer was read as White's clock: 2427 s for 180) - first test: share of consecutive seconds unchanged < 0.15 | Arena timer 0.00, but real clocks that read poorly while idle scored 0.06-0.10: on 124 games clocks OK 77 → 72 (real clocks dropped) | **No** |
| ...test instead the longest non-stop countdown as a share of the game (`clock_read.longest_tick_share` > 0.5) | Arena timer 0.92, real clocks ≤ 0.09. On the 12 games the two tests disagreed on: arena games' wrong values become missing (3fS-8ke1eaQ-1: 45 wrong → 1), the real clocks are kept (BdcY0WCN29Y-6 74/74) | Yes |
| Re-cut the piece templates when a re-fit moves the board box (`scan.RECALIBRATE_ON_REFIT`): replace them | Aig9-ZyQ8Ak (black king missing from 45% of readings, the calibration box 4 px high): games 9 → 2, white king missing 1% → 26% | **No** |
| ...keep the first calibration's templates and add the latest cut beside them (`scan.RECALIBRATE_ON_REFIT`) | Aig9-ZyQ8Ak: black king missing 45% → 2.8%, matched games 7 → 21, 0 wrong trusted. n6ftt1LDO24 (worst in the batch, kings missing 46%/71%): → 19%/27%, games 0 → 2 (1 matched, 23/23 right), 0 wrong trusted | Yes (on by default from 2026-10-19; readings cached before then don't have it) |
| Validate clock candidates on 9 frames, not 1 (lichess dims the idle clock) | lAniuIChYuA clocks 40/83 → 80/83 | Yes (`954b3aa`) |
| Screen 4 candidates by OCR coverage; pair by vertical alignment; assign sides by relative position | Fixed T-shirt text, stacked panels, arena timers | Yes (`2844a88`, `9edf182`) |
| Keep only physically possible readings (no rise > increment) | Removes repeated misreads of dimmed digits | Yes (`38d2053`) |
| ...with drops limited to elapsed time | Sites lag-correct the clock (361→351 in 1s), so this blanked correct stretches | **No**, drops allowed up to +30s |
| Sample the mover's clock just before the next ply | Helped some games, hurt others | Replaced by the next row |
| Mover's clock = most common value between their move and the next ply | Most robust | Yes (`38d2053`) |
| Read a low-contrast idle clock from inside its widget | One game's clock 46% → 100% of frames read | Yes (`0dd2ed5`) |
| Grow clock boxes vertically; OCR-based candidate validation; coverage threshold 0.35 | 18 of 24 games missing a side fixed | Yes (`0d8a052`) |
| Clock candidates pooled from the whole game window *and* its first four minutes | Over a long game other changing UI near a clock merged with it into one wide blob reading nothing (GingerGM stream: a side missing in 2 of 4 games); over the opening minutes it was found cleanly. Both sides found on the traced game | Yes |
| Board exclusion margin in `find_clock_regions` 10 px → 3 px | chess.com's current layout puts the bottom clock 4 px under the board; the narrator's clock was missing in every game of the Titled Tuesday streams (clocks OK 8/22). Both clocks found on the traced game; existing clock tests unchanged | Yes |

## Ratings

| Idea | Result | Kept? |
|---|---|---|
| Both sides read the same rating: blank the opponent's (`catalog.reconcile_ratings`) | 6 of 337 games; the opponent's was wrong in all six. Wrong rating values 27 → 21 of 674, one right one lost (hQzhibR0ZYg-2, players 18 apart, both read 1129) | Yes |
| Lichess: read the row beside each clock | Lichess games with both ratings right 2 → 4 of 6 (7 of 7 after later fixes) | Yes (`9edf182`) |
| ...for clocks above/below the board too | Read the board itself ("2990", "199") | **No**, side panels only (`627e115`) |
| Vote across board-box shifts (−3/0/+3 px) | The band reader flips on a few pixels; 4 → 6 of 6 | Yes (`a34e821`) |
| Band height scaled with board size (1080p) | "(1339)" read as 1959 before | Yes (`bc421b7`) |
| Four glyphs between parens = four digits | "(2742)" no longer 742 | Yes (`ee62d37`) |
| ...expected digits from span *width* | Font-dependent; "(150)" read as 1150 | **No** |
| Drop a 3-digit rating paired 700+ below a 4-digit one | Partial reads become missing, not wrong | Yes (`ebda1a0`) |
| Per-video reconciliation using the narrator's side (`catalog.reconcile_ratings`): the narrator's rating is one account all video long, so a read >250 from the video's median (of 4-digit reads, when two exist) is blanked; a narrator at 1500+ isn't paired with an opponent under 1000 | 14 wrong reads blanked, 0 right ones lost: wrong sides 32 → 18 of 272 (11.8% → 6.6%). Taking the median over *all* reads blanked a right 2747 on the blurry BdcY0WCN29Y (junk outnumbered real reads) | Yes |
| Whole-line Tesseract read of the band ("(NNNN)" by regex) next to the glyph path; combined: either alone, agreement, or *nothing* on disagreement | One frame of 28 games (56 sides): glyph 38 right/4 wrong/16 missing, line 41/4/13, combined 42/2/12. Bartholomew's skin (glyph read "(2723)" as 2235 at 0.96) now comes out missing instead of wrong | Yes |
| Tesseract digit whitelist | No change | No |
| lichess: shrink each clock box to its tallest text line (the digits) before reading the rows beside it (`rating_read._digits_box`). Motion-found boxes take in the time bar and name row (Jqz9fdHOrPQ: 152x81 around a 37-px clock), so the reader looked past the row it wanted - most lichess misses were unread sides (237), not wrong ones (68) | Rescored 525 lichess games (ratings only merged into the catalog; the rescore path itself moves clocks/moves by ±2 games): lichess both sides right 55.0% -> 70.3%, all games 77.0% -> **81.8%**; sides missing 339 -> 220, **wrong 129 -> 138** (some are the narrator's rating read for the opponent; per-video reconciliation blanks most: without it, wrong 209) | Yes |
| Contrast stretch / CLAHE fallbacks | Didn't help; CLAHE produced garbage | **No** |

## Ground truth

- `someonecallanambulance` on chess.com is Rosen's Tricks & Traps account
  (it's not lichess). Found by reading the username off a frame. Matched games
  63 → 112.
- lichess per-user export: curl's default User-Agent gets a 404, and it has
  since returned `429 "Please only run 1 request(s) at a time"`, even with a
  token. Unresolved. Game links in video descriptions still work.

## Known open problems

- Premove bursts: the in-between position is never drawn, so they stay
  untrusted.
- In 6 traced errors the recognizer read the real position exactly but
  reconstruction chose a different path. Not yet investigated.
- Ratings: blurry low-res broadcasts (BdcY0WCN29Y); provisional new-account
  ratings; ±2 games of run-to-run noise from re-downloaded hi-res frames.
