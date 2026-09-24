# Scanner experiments log

What's been tried to make the scanner more accurate, what it measured, and
whether it was kept. Read this before trying an idea, since several obvious
ones have already failed for non-obvious reasons. Add to it as you go.

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

Live catalog, 119 games matched to real games: **0 wrong trusted moves**
(of 4,195 trusted plies), 73% of recorded plies trusted, full 15-move
opening trusted in 75 games, every move right in 67%, clocks right in 88%,
both ratings right in 74%. (On 2026-09-22: 41 games had a wrong move shown,
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
| Re-read a poorly matching occupied square at small offsets (±4 px) | A bishop read as empty matched at 0.83 instead of 0.14. On 3 videos: KfuyDm5uksI 3 → 9 games found, lAniuIChYuA-1 all 96 plies right (was 83/88), trusted plies 168 → 375, still 0 wrong | Yes (see git log) |

## Trusted moves (`trust.py`)

Rule: stop one ply before a bunch of 3+ plies (or any bunch past ply 30),
and hold back the last 2 plies (4 past ply 30) unless a clock-verified mate.
Tuned on the 119 benchmarked games, so expect a small error rate on new ones.

| Idea | Result | Kept? |
|---|---|---|
| Stop at the first bunch of 2+ | 2/119 wrong games but median only 13 trusted plies | No |
| Hold back fewer end plies when both clocks were read | +17 full openings but 5 wrong last plies came back | **No** |
| Stop where the mover's clock *rose* | Fires on increment games (600+8) too; cost 6 openings, caught 1 error | **No** |
| Flag moves after which the mover's clock kept running | Flagged 19 all-correct games, caught 13 of 38 errors | **No** |

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
