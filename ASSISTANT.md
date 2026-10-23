# The assistant: an AI opening and position instructor

Design notes for where this project is heading. Nothing here is built yet. The scanner and
the database page come first, because the assistant is only as good as the data under it.

## Why

A grandmaster explaining *why* they play a move, what they're worried about and what the
position is really about is the most useful thing a player can hear, and it's very hard to
get without a strong teacher. The narrators on YouTube say exactly this out loud, game after
game, as intuition rather than engine lines. The assistant's job is to collect what they've
said and give it back when someone asks about a position.

The bet is that commentary is good on average: it comes straight from an expert's intuition.
An engine can help catch the times it isn't (see below), but the value is the explanation,
not the evaluation.

## What we have to build on

Per video, in `scanner/cache/` (local only; see the README's "Data and licence"):

- **The games**, read off the screen: moves, clocks and ratings, with the second each move
  appears in the video (`move_seconds`). Every move carries a trust mark, and only moves the
  scanner can vouch for are shown.
- **The transcript**: the narrator's captions as timed segments
  (`transcripts/<video_id>.json`). Lining these up with `move_seconds` ties what was said to
  the position on the board at that moment.
- **The real games**: about 93% of scanned games match a game in the narrators' chess.com
  or lichess archives (`truth/`). The matched game's link will be kept per video as
  `<video_id>-truth.json`, so the assistant can use the exact record: full moves, result,
  opponent.

Videos with no real game behind them work the same way. The scanner reads the moves,
times them and decides what to trust from the video alone. The real games are there to
measure it.

## Design

### Group by position

Commentary is filed under the **board position** (FEN without the move counters), not under
the game or the move order. Every route into a position adds to the same pile, so
transpositions are handled for free, and the same position reached in many videos gathers
many explanations.

### Group middlegames by structure too

Exact middlegame positions rarely repeat, but pawn structures do: an isolated queen's pawn,
Carlsbad, a London setup, a closed centre. Filing commentary under the structure as well (the
pawn skeleton, perhaps with the kings' sides) lets plans like "in this structure, White
plays for the minority attack" build up across very different games.

### Distil across videos

The hope is that many videos reach the same positions, so what's been said about each can be
distilled into expert-level understanding:

- **Agreement is confidence.** One remark from one narrator is an anecdote. The same idea
  from several masters in different games is close to real understanding.
- **Disagreement is interesting.** It often marks a genuinely double-edged position, and is
  worth presenting as such rather than averaging away.
- **Weight by context.** A comment made with time to think (an opening, a stream between
  games, a post-game review) is worth more than one made in a time scramble.

### Score claims with an engine

Blitz commentary is sometimes simply wrong ("this is winning!" when it isn't). Checking
concrete claims against an engine (an evaluation, a tactic, "this loses a piece") can flag or
down-weight them. The engine checks claims; it doesn't replace the explanation. A wrong
claim can still carry the right idea about the position.

## Where the commentary is

- **Blitz commentary is sparse.** Often it's just around one or two key moves, and it thins
  out later in a game, when time is low or the position is less well understood.
- **Most valuable: well-known opening positions**, then **well-known middlegame
  structures**.
- **Endgames** are well documented already and may be out of scope.
- **Hyper-specific late positions** are worth less, but use what we can.

The hard part is making it general: filling in a wide range of openings and positions, not
just the narrators' favourites.

## Sources

- **Eric Rosen first, all of it.** He plays a lot of different openings, so he's a very good
  start for breadth as well as depth. Target every video on his main channel and every
  archived stream VOD on **@EricRosenExtra**, which hasn't been scanned yet. Live VODs
  already work: 46 of the 215 videos cached so far are stream VODs, including the two-hour
  Titled Tuesday streams. His chess.com and lichess accounts are already in the real-game
  archives, so most of those games should verify.
- **Naroditsky's speedrun series, all of it (2026-10-21).** Stream commentary turned out to be
  mostly move-calling; his speedruns are made to teach, explaining moves and openings as he
  plays. He doesn't re-explain basics every time, so opening coverage needs the whole series
  (388 videos on his channel, ~213 hours), not a sample. Scanned in batches, each ordered to
  add the openings not yet covered (the titles name them).
- **Candidate channels (researched 2026-10-22, for openings the four don't cover):** IM Alex
  Banzea (~280 rating-climb videos: London, Jobava, KID, KIA, Caro-Kann), chessbrah / GM Aman
  Hambleton (~250 opening speedruns: London, KID, Queen's Gambit, Colle, English), Chessexplained
  / IM Christof Sielecki (~2,400 one-game blitz videos with live comments, mostly 2011-2016 on
  ICC/Playchess/chess24 boards: a test of reading other sites), ChessDojo / IM Kostya Kavutskiy
  (~55 speedruns, 1.d4 and KID), GMHikaru's educational "Slowkaru" speedruns. Pilot a few videos
  of each (board read, games matched, instructive share) before scanning a channel in bulk.
- **Then as many master-level players as possible.** Each narrator's coverage is deep along
  their own repertoire and thin elsewhere, so breadth comes from more channels with
  different repertoires, not just more videos from the same ones. Today: GingerGM, John
  Bartholomew and Daniel Naroditsky, as well as Eric Rosen.

## Copyright and sources

The transcripts are the channels' own work, so they stay local and out of the repo. What
the assistant publishes should be built from them, never the transcripts themselves, and
every idea it gives should cite and link the video and timestamp it came from, so the
assistant sends people to the creators.

**Paid courses and books are out.** Distilling them is much closer to taking the product than
distilling commentary given away for free, and it would be controversial at a time when
opinion on AI and training data is split. Free, public commentary only. Eventually it may be
worth asking the narrators directly (Eric Rosen first): their blessing would remove most of the
risk, and they might like the idea.

More data will be needed. These players play a lot of chess and don't comment on every move,
so a common position is commented on rarely. That argues for more channels and the stream
archives (@EricRosenExtra), not paid material.

## First steps

The scanner is limited by CPU; the assistant mostly isn't (data work and language-model calls on
the API; engine scoring is the exception, capped at 1-2 cores). So the assistant is built in the
gaps while scanner batches run, without slowing the scanner down.


1. **Measure coverage:** for each position after N moves, how many videos and narrators reach
   it. That shows where commentary is already dense and where it's thin.
2. **Line commentary up with positions:** for each transcript segment, the position on the
   board (and the last move) at that time, allowing for commentary that lags a move by
   seconds or comes before it ("I'm going to play Nf3 here").
3. **A first prototype, by hand:** for the 5-10 densest positions, gather everything said
   within a few seconds of the position appearing, across all videos, and write the
   distilled summary in a Claude session. That shows whether there is real insight in there
   before anything is automated, and shows the problems (mangled notation, lagging
   comments, small talk) early.
4. **If it's promising,** automate step 3 with language-model calls, citing the videos and
   timestamps each idea came from.
5. **Then** add structure grouping, engine scoring and weighting.

## Findings

### 2026-10-20: first look (`assistant/positions.py`, `assistant/commentary.py`)

On the rebuilt catalog (1,718 games, 193 videos, trusted moves only):

- **Coverage is good where the narrators' repertoires are.** The London with ...c5 and
  ...Nc6 is reached in 31 videos (45 games). The Slav, the QGD Exchange, the Stafford and
  several London lines turn up in 10-20 videos each. Transpositions meet as intended. Nearly
  all of the top positions are Eric Rosen's.
- **Commentary on opening positions is mostly chatter.** Of 45 visits to that London position,
  about 5 say anything about the opening ("my anti-London line", "everyone's avoiding it with
  knight f3 on move two"). The rest is "London time", standings and chat. Reading from each
  game's first move (`--from-start`) for the QGD Exchange gave the same: a few nuggets ("my
  usual QGD bread and butter", "h6 walks into tactics") among stream talk.
- **Why:** in streams the narrator plays the opening from memory and talks to chat. The
  explanations likely come once the game leaves their preparation and they have to think,
  and in recorded videos made to explain. So: look at what's said in the middlegames that
  follow an opening, not at the opening positions themselves, and compare streams with
  recorded videos.

First targets: **the London** (296 games in 77 videos, nearly all Eric Rosen as White) and **the
Sicilian** (161 games in 99 videos: Eric Rosen on both sides, GingerGM, John Bartholomew and
Naroditsky as Black). Cat plays both, so can judge whether a summary is any good. The Sicilian
has more narrators, so it's the better test of distilling across people.

**A random sample says the data is useful, just not at the opening positions.** 60 random
visits to trusted positions across all games and phases, each position's own time on the
board, sorted by hand (59 had something said):

| Kind | Share |
| --- | --- |
| Instructive: plans, ideas, why a move, evaluations ("I'm trying to open up the b-file. Why is it important…") | ~44% |
| Move-calling or a bare evaluation ("Okay, let's take on h5. Queen b3.") | ~32% |
| Chatter | ~24% |

The chatter is concentrated in the first ~8 plies. From the early middlegame on, the narrators
mostly think aloud, and the recorded videos (GingerGM, Naroditsky) explain more than the
streams. So a given position is commented on rarely, but plans and ideas recur: grouping by
structure and plan, not only exact position, is what should make this work. Opening knowledge
shows up occasionally ("there's some theory in this line, h5 is good to know"; Magnus playing
Ne2 in the Kinder Surprise). The sample is small, so the shares are rough.

Next: measure where in a game the instructive commentary is (by ply and by video type),
using a cheap model to sort segments into instructive and chatter (a z.ai GLM-5.3-flash key
is available, kept in an environment variable, never in the repo).

### 2026-10-21: sorting with GLM-5.3-flash (in progress)

- `assistant/units.py VIDEO_ID`: each game's commentary cut into **units**, what was said while
  one position was on the board (a segment goes to the position at its midpoint). 57,004 units
  in 193 videos, 28,838 from trusted plies. Most units are fragments of a sentence.
- `assistant/classify.py VIDEO_ID ...`: sends units to z.ai in batches of 12 consecutive units of
  a game, and labels each as instructive, move-calling or chatter. Instructive units also get
  `topics` and a one-line `idea` in clean SAN. Output goes to `assistant/cache/labels/<video>.json`
  with token counts. Needs `ZAI_API_KEY` exported (pay-as-you-go key). The User-Agent is
  `$ASSISTANT_USER_AGENT` (default `whittle`) and the number of requests in flight is
  `$ASSISTANT_WORKERS` (default 2).
- **API quirks:** GLM-5.3-flash always thinks, so the script sends
  `"thinking": {"type": "enabled", "effort": "low"}` (`disabled` is refused). 30-unit
  batches took over 3 minutes, past the timeout. 6 requests in flight hit "1302 Rate limit
  reached", so the default is now 2. That's untested: find the highest that works.
- **One video done** (85-Uv_kRoFw, 1 game, 19 units): 1,585 in / 5,813 out tokens (most
  of it thinking), about $0.003 at the list price ($0.15/M in, $0.50/M out, secondhand; check
  z.ai's own page). The labels look right: the Dutch d3 line, Black's plan of developing the
  bishop, castling and ...Qg6, and the ...d4 pawn sac with its reason.
- **Validation set:** the first one (80 units labelled by hand) was lost. See below.

### 2026-10-21: Claude Haiku 5.5 instead of GLM

GLM was dropped (always thinks, rate-limited at 6 requests, slow). Claude models now run through
the Anthropic API (key in `HAIKU_API_KEY`, kept apart from `ANTHROPIC_API_KEY` so other tools
don't pick it up; prepaid credits);
`classify.py` defaults to `claude-haiku-5-5` at effort low with a JSON schema for the labels.

- **New validation set** (`assistant/validate.py`): 80 random units (seed 20261021) in
  `assistant/cache/sample.jsonl`, labelled by hand (drafted by Claude) in `sample_hand.json`
  before any model saw them: 22 instructive, 48 move-calling, 10 chatter. `validate.py label`
  sends each unit in the same 12-unit batch `classify.py` would; `score` compares.
- **Results** (instructive or not is what matters: move and chatter are both discarded):

  | Run | 3-way | Instructive or not | Precision | Recall | All units |
  | --- | --- | --- | --- | --- | --- |
  | Haiku 5.5, first prompt | 64% | 76% | 16/29 | 16/22 | ~$2.40 |
  | Haiku 5.5, tighter prompt | 69% | **85%** | 15/20 | 15/22 | ~$2.40 |
  | Sonnet 5.5, tighter prompt | 74% | 88% | 17/22 | 17/22 | ~$45 |

  The tighter prompt (instructive only if the unit itself states a reason, idea or plan, with
  near-miss examples written fresh, not taken from the sample) fixed Haiku's main error,
  calling bare evaluations instructive. Most of what's left is move vs chatter, partly the
  hand labels' own inconsistency ("you idiot" labelled move; the prompt now says chatter).
  Caveat: the hand labels and both models are Claude, so Cat spot-checking some disagreements
  would make these numbers more trustworthy.

### 2026-10-21 (night): ideas must be the narrator's own

The first full run (all 193 videos, ~$2.40) labelled 57,004 units: about 30% instructive. Cat's
first look at the review page (`assistant/review.py`, local) found an idea that was right but
not said: over waffle ("so what am I doing… if I move my rook up so I can swing it"), Haiku wrote
a clean plan of a queen-and-rook attack, read off the moves. Every idea will be cited to a
creator's video, so it must be theirs.

- **Evidence:** instructive labels now quote the words they rest on; the quote is checked
  against the transcript (it may run into the neighbouring unit, since captions cut sentences
  across positions, and may skip a repeated phrase).
- **Dev set** (`assistant/iterate.py`): 60 units, 15 per channel, 20 per phase, never from the
  validation set; each prompt version is labelled (~5 cents) and reviewed on a page with a
  second verdict per idea: faithful, or beyond what was said.
- **v2** (evidence, "only what the narrator said"): of 16 instructive, about 8 reasonable, 5
  added things nobody said ("helps defend the king", "c4 and doubling on the d-file"), several
  fragments, and junk in ideas ("(hedged)").
- **v3** (one sentence, no added reasons or consequences, squares pointed at but not named stay
  unnamed, fragments and memory talk not instructive): 14 instructive, fewer inventions; the
  "here/this square" ideas stay vague. A few still add detail ("if the attack continues").
  Trailing junk after the sentence is now cut in code.
- Deictic speech ("go here, do this") is common in streams; the narrator is drawing arrows.
  Reading those (`scanner/arrows.py`, see EXPERIMENTS.md) would make such units concrete.
- Reliability fixes: a reply looping on "]}]}" until max_tokens is retried, and units a
  reply leaves out are asked for again.

### 2026-10-22: Naroditsky's speedruns are twice as instructive

All 253 videos relabelled with prompt v3 (Haiku 5.5, $4.85: more videos, a longer prompt and
retries than the $2.40 estimate). Share of units labelled instructive with their evidence found
in the transcript:

| Source | Videos | Units | Instructive | Opening / middlegame / late |
| --- | --- | --- | --- | --- |
| Naroditsky: Theory speedrun | 33 | 1,494 | 55% | 47% / 65% / 49% |
| Naroditsky: original speedrun | 15 | 1,254 | 52% | 46% / 61% / 43% |
| Naroditsky: Sensei speedrun | 10 | 225 | 50% | 45% / 61% / - |
| GingerGM | 38 | 10,703 | 28% | 15% / 37% / 25% |
| John Bartholomew | 37 | 14,402 | 28% | 13% / 35% / 26% |
| Eric Rosen | 95 | 27,782 | 24% | 16% / 29% / 20% |
| Naroditsky: streams and blitz | 7 | 1,438 | 10% | 4% / 14% / 8% |

The format matters more than the person: Naroditsky's own streams are the least instructive
source. In the opening his speedruns are three times as instructive as the streams, which is
where the assistant needs it. Sample ideas from the Theory speedruns: "In the Accelerated
Dragon, Black delays ...d6 to avoid transposing into the main-line Dragon"; "Black's ...e5
comes with tempo and produces a King's Indian-style structure". 142 trusted opening ideas from
33 Theory videos so far. Run-on text after an idea still slips through now and then.

### 2026-10-22: first distilled opening guides

`assistant/distill.py`: for an opening (games starting with its moves), every instructive
idea from a trusted ply up to ply 30 with its evidence found in the transcript, written up
by Sonnet 5.5 as a short study guide built only from those ideas, each point citing them
(`assistant/cache/summaries.html`, local: citations link to the video moment). Prototype on
three openings, ~$0.20 in all: the London (457 ideas, nearly all Eric Rosen), the Alapin
Sicilian (68, mostly Naroditsky) and the Caro-Kann (183). The Alapin guide reads like a real
one: Black's main systems, the 2...Nf6 3.e5 Nd5 4.d4 cxd4 5.Nf3 move order and why, the
2...d5 lines with their plans and a trap, practical lessons; a few points stay vague ("a
course recommends a different idea"), and any error in an idea carries through. For Cat to judge.

Next:
1. Cat reviews v3 (`assistant/cache/review_v3.html`); iterate; then relabel everything (~$2.40; the Batch API would halve
   it), and measure the instructive share by ply, narrator, and stream versus recorded video.
2. Group the `idea`s by opening and pawn structure, and draft London and Sicilian summaries
   with citations for Cat to judge (Sonnet 5.5 reading the instructive units again).

### 2026-10-23: the Naroditsky speedrun archive

Every speedrun video on Naroditsky's channel has been scanned (388 videos, in seven batches,
2026-10-21 to 2026-10-23), merged into the catalog only when the benchmark found no wrong
trusted move, and its commentary labelled with prompt v3. With his streams and other videos
the channel now gives 615 games from 316 videos, 309 of them listed (12+ trusted plies).

| Series | Videos | With games | Games | Listed | Matched (every move right) | Instructive (opening / middlegame / late) |
| --- | --- | --- | --- | --- | --- | --- |
| Original rating speedrun | 167 | 140 | 266 | 164 | 219 (186) | 50% (43% / 57% / 42%) |
| Sensei speedrun | 118 | 82 | 106 | 49 | 83 (67) | 51% (43% / 61% / 44%) |
| Theory speedrun | 42 | 35 | 60 | 27 | 34 (29) | 55% (47% / 65% / 49%) |
| Develop Your Instincts | 40 | 36 | 95 | 25 | 17 (12) | 54% (48% / 64% / 39%) |
| Beginner to Master | 21 | 15 | 29 | 13 | 21 (16) | 55% (47% / 65% / 47%) |
| Streams and other videos | 8 | 8 | 59 | 31 | 53 (45) | 10% (4% / 14% / 8%) |

(Matched: games found in the real game archives of his accounts SenseiDanya, OhMyLands,
danielnaroditsky and FrankfurtAirport. Instructive: share of labelled units with their
evidence found in the transcript; opening is up to ply 16, middlegame up to ply 50.)

Every speedrun series is about half instructive, and the opening share holds at 43-48%
across them: the result from the first 58 videos (2026-10-22) holds on six times as many.
Openings in the speedrun games with a trusted first move (454): 1.e4 e5 116, 1.e4 c5 112,
1.d4 Nf6 50, 1.d4 d5 31, 1.e4 c6 30, 1.e4 d5 and 1.e4 e6 18 each, 1.e4 g6 15. He plays
White in 305 games and Black in 246.

Weak spots:
- Sensei and DYI games are listed least often (46% and 26% of their games). The DYI games
  rarely match a real game (17 of 95), so trust can't be checked against the archives
  there; the account used for those videos is still to be found.
- Speedrun videos with no games: of seven, five now give games (two boards in one video:
  the speedrun on one, his old tournament games in a database on the other; and a
  full-height blue board at 720p that never calibrated; see scanner/EXPERIMENTS.md). Left:
  a Puzzle Rush video (no games to find) and one full-height-board video. That layout
  still reads poorly once calibrated (one game from a video with several).

Next: re-run `distill.py` with the full archive, and the channel pilot (Banzea, chessbrah,
Chessexplained, ChessDojo) for the openings Naroditsky plays less.

Later, in the web app (Cat, 2026-10-23): bake the assistant's output offline into a file the
page loads, and show it below the search board for the position on it: what the masters said
about this position and its moves, each point cited to its game, narrator and video moment,
so clicking through the database's next moves also walks through the commentary. Only the
model's paraphrased ideas and the links would be published. No transcript text: the evidence
quotes stay local (see Copyright and sources).
