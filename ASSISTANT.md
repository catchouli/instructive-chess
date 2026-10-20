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
- **Then as many master-level players as possible.** Each narrator's coverage is deep along
  their own repertoire and thin elsewhere, so breadth comes from more channels with
  different repertoires, not just more videos from the same ones. Today: GingerGM, John
  Bartholomew and Daniel Naroditsky, as well as Eric Rosen.

## Copyright

The transcripts are the channels' own work, so they stay local and out of the repo. What
the assistant publishes should be built from them, never the transcripts themselves.

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

Next: measure where in a game the instructive commentary is (by ply and by video type),
using a cheap model to sort segments into instructive and chatter (a z.ai GLM-5.3-flash key
is available, kept in an environment variable, never in the repo).
