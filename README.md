# Instructive Chess

**Live site: https://catchouli.github.io/instructive-chess/**

<p>
  <a href="screenshots/home.png"><img src="screenshots/thumbs/home.png" width="32%" alt="Catalog of recent games"></a>
  <a href="screenshots/search.png"><img src="screenshots/thumbs/search.png" width="32%" alt="Searching by opening moves"></a>
  <a href="screenshots/game.png"><img src="screenshots/thumbs/game.png" width="32%" alt="Stepping through a game"></a>
</p>

A catalog of **instructive chess games** — games played on YouTube by masters who narrate
every move — that you can search by the moves played.

Two parts:

| Part | What it is |
|------|------------|
| `scanner/` | A Python tool. Give it a YouTube URL; it downloads the video, reads the on-screen board with computer vision, reconstructs each game move-by-move, and appends the games to `web/data/games.json`. |
| `web/` | A dependency-free static site. Loads `games.json`, lets you type PGN, and lists games whose opening matches. |

All games are assumed to start from the standard starting position.

---

## Web app

Nothing to build. Serve the folder:

```sh
python3 -m http.server -d web 8000
# open http://localhost:8000
```

- Empty search box → the 20 most recent games.
- Type moves (`1. e4 c5 2. Nc3`, or just `e4 c5 Nc3`) → every game whose mainline **begins
  with exactly those moves**. An illegal move filters on the legal prefix and says so.
- Filters: **Narrator plays** (White / Black) — the point is to watch the *narrator* play the
  line you searched for, not their opponent; **Min rating** (both players at least this;
  games with no readable rating are excluded while the filter is active); **Exclude
  speedruns** (the narrator climbing a fresh account against much weaker opponents).
- Each card names the side the narrator played, and expands to a step-through board, the
  clocks and ratings read from the video, and the full PGN. Each result links to the video
  at the game's timestamp.
- Only the moves the scanner is sure of are shown and searched (see `scanner/trust.py`):
  measured against the real games, none of those moves is wrong, and the whole 15-move
  opening is shown for a little under half of the games.

The only runtime dependency is `web/vendor/chess.js` (MIT), vendored locally — the page
makes no network requests.

---

## Scanner

### One-time setup

```sh
python3 -m venv scanner/.venv
scanner/.venv/bin/pip install -r scanner/requirements.txt
brew install ffmpeg          # video decoding / muxing
brew install tesseract       # OCR engine used to read on-screen clocks and ratings
```

### Catalog a video

```sh
scanner/.venv/bin/python scanner/scan.py "https://www.youtube.com/watch?v=ao5Xxv7HqQ8"
```

This writes new entries into `web/data/games.json` (re-scanning a video replaces its
entries). Add `--dry-run` to print the games without touching the file. A full-length
video takes a few minutes (download + ~1 fps frame scan + reconstruction).

### How it works

1. **Download** — `yt-dlp` fetches the video (≤720p is plenty) into `scanner/cache/`;
   `ffmpeg` decodes it down to ~1 fps of stills.
2. **Locate the board** — a coarse 8×8-checkerboard search plus gradient-based grid
   refinement finds the board rectangle.
3. **Self-calibrate** — on the first frame that shows an opening setup, the board box is
   nudged onto the pieces and the 32 pieces are cropped straight out of the video and used
   as templates. Same renderer, same theme, same scale, so later matching (masked
   cross-correlation) is near-exact and immune to square colour and last-move / check
   highlights. Because the templates come from the video itself, any digital piece set
   works with no extra assets.
4. **Read every frame**, majority-vote each square over a short time window to kill
   flicker.
5. **Reconstruct** — replay legal moves from the start position, bridging small gaps and
   stepping over noisy frames, until a game can't be continued or a new opening appears.
   If a video skips the pristine starting position (common in recap videos), a game is
   still opened from any position that's a short *unique* continuation of the standard
   start. Correctness is favoured over length: an unresolved position ends the game
   rather than guessing, and `confidence` reports the fraction of moves resolved with no
   ambiguity.

### Useful flags

| Flag | Purpose |
|------|---------|
| `--dry-run` | Print games; don't write `games.json`. |
| `--fps 2` | Sample more frames (helps on fast games; slower). |
| `--start / --end SECONDS` | Scan only part of the video. |
| `--calibrate` | Write `scanner/cache/<id>-calib.png` with the detected board boxed, then exit. |
| `--crop x,y,w,h` | Skip auto-detection and use this board box. |
| `--min-confidence 0.7` | Drop reconstructed games below this confidence. |
| `--speedrun` / `--no-speedrun` | Tag the video's games as (not) a speedrun; by default decided from the title. |
| `--catalog PATH` | Write to a scratch catalog instead of `web/data/games.json`. |

Each game records which colour the narrator played (`pov`, from the board's orientation -
a player's own live board has their side at the bottom) and whether the video is a
speedrun. `scanner/catalog.py` re-derives both tags for an existing catalog without
rescanning.

### Scope

Tuned for **digital boards** (lichess, chess.com, analysis GUIs) shown head-on. The piece
set and board theme don't matter — templates are learned from the opening frame of each
video. What it needs is a video that opens on the standard starting position and a board
that stays put.

### Tests

```sh
scanner/.venv/bin/python -m pytest scanner/tests/   # scanner: move reconstruction
node web/lib.test.mjs                               # web: query parsing + matching
scanner/.venv/bin/python scanner/tests/benchmark.py # accuracy of the whole catalog vs the real games
```

`tests/benchmark.py` matches each catalogued game to its real game (linked from the video
description, or in the chess.com archives of the channel's known accounts) and scores the
moves, clocks, ratings and narrator side. To see *why* a video's games came out the way they
did, `tests/dump_readings.py VIDEO_ID` caches the per-frame board readings and
`tests/trace_video.py VIDEO_ID --readings` replays them with a trace, showing each game
against its real game and the readings around the first wrong move.
`scanner/EXPERIMENTS.md` logs what has been tried.
