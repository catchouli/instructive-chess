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

## Current state (2026-09-24)

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
| Reconstruction gets very slow on a video whose readings are persistently a square off: each unexplained reading burns the full 100,000-node budget | not addressed; the box re-fit removes the cause on that video | |

Also found: John Bartholomew is chess.com `Fins0905` (not `Fins`), Simon
Williams `Ginger_GM`. His video ZzwYoNjK6_Q: 8/8 games matched, 6 every
move right, but **ratings 0/8** (small "(2533)" font; reads like 268, 3274)
and clocks 4/8 - open.

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

## Clocks

| Idea | Result | Kept? |
|---|---|---|
| Tesseract OCR instead of self-calibrating digit templates | Self-calibration was confidently wrong for whole games | Yes (`5b80713`) |
| Validate clock candidates on 9 frames, not 1 (lichess dims the idle clock) | lAniuIChYuA clocks 40/83 → 80/83 | Yes (`954b3aa`) |
| Screen 4 candidates by OCR coverage; pair by vertical alignment; assign sides by relative position | Fixed T-shirt text, stacked panels, arena timers | Yes (`2844a88`, `9edf182`) |
| Keep only physically possible readings (no rise > increment) | Removes repeated misreads of dimmed digits | Yes (`38d2053`) |
| ...with drops limited to elapsed time | Sites lag-correct the clock (361→351 in 1s), so this blanked correct stretches | **No**, drops allowed up to +30s |
| Sample the mover's clock just before the next ply | Helped some games, hurt others | Replaced by the next row |
| Mover's clock = most common value between their move and the next ply | Most robust | Yes (`38d2053`) |
| Read a low-contrast idle clock from inside its widget | One game's clock 46% → 100% of frames read | Yes (`0dd2ed5`) |
| Grow clock boxes vertically; OCR-based candidate validation; coverage threshold 0.35 | 18 of 24 games missing a side fixed | Yes (`0d8a052`) |

## Ratings

| Idea | Result | Kept? |
|---|---|---|
| Lichess: read the row beside each clock | Lichess games with both ratings right 2 → 4 of 6 (7 of 7 after later fixes) | Yes (`9edf182`) |
| ...for clocks above/below the board too | Read the board itself ("2990", "199") | **No**, side panels only (`627e115`) |
| Vote across board-box shifts (−3/0/+3 px) | The band reader flips on a few pixels; 4 → 6 of 6 | Yes (`a34e821`) |
| Band height scaled with board size (1080p) | "(1339)" read as 1959 before | Yes (`bc421b7`) |
| Four glyphs between parens = four digits | "(2742)" no longer 742 | Yes (`ee62d37`) |
| ...expected digits from span *width* | Font-dependent; "(150)" read as 1150 | **No** |
| Drop a 3-digit rating paired 700+ below a 4-digit one | Partial reads become missing, not wrong | Yes (`ebda1a0`) |
| Per-video reconciliation using the narrator's side (`catalog.reconcile_ratings`): the narrator's rating is one account all video long, so a read >250 from the video's median (of 4-digit reads, when two exist) is blanked; a narrator at 1500+ isn't paired with an opponent under 1000 | 14 wrong reads blanked, 0 right ones lost: wrong sides 32 → 18 of 272 (11.8% → 6.6%). Taking the median over *all* reads blanked a right 2747 on the blurry BdcY0WCN29Y (junk outnumbered real reads) | Yes |
| Tesseract digit whitelist | No change | No |
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
