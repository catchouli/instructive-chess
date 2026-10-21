"""Score the whole catalog (web/data/games.json) against real game records.

Every reading the scanner produces - moves, clocks, ratings - has an exact
answer in the real game's PGN: the SAN moves, a [%clk] after every move, and
WhiteElo/BlackElo. This finds that real game for each catalogued game and
reports how often each reading is right, so accuracy work can be measured
instead of spot-checked.

Where the real games come from:
  1. Game links in the video's own description (cached .info.json) - lichess
     and chess.com. Exact, and the only source for lichess-only videos.
  2. The monthly archives of the channel's known accounts (chess.com: see
     ACCOUNTS; lichess: see LICHESS_ACCOUNTS, needs an API token), for games
     played in the months before the video went up.

A catalogued game is matched to the real game sharing the longest opening
prefix with it, and only if that match is long enough and unambiguous - a
short, common opening could otherwise "match" an unrelated game.

Usage:
    scanner/.venv/bin/python scanner/tests/benchmark.py            # summary + worst games
    scanner/.venv/bin/python scanner/tests/benchmark.py --all      # every game
    scanner/.venv/bin/python scanner/tests/benchmark.py --json out.json

Network is only needed the first time a month or linked game is seen;
finished months and linked games are cached under scanner/cache/truth/.
"""
from __future__ import annotations

import argparse
import difflib
import functools
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import chess.pgn

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "web" / "data" / "games.json"
VIDEO_CACHE = ROOT / "scanner" / "cache"
TRUTH_CACHE = VIDEO_CACHE / "truth"

# chess.com accounts whose games appear on each channel.
ACCOUNTS = {
    "Eric Rosen": ["imrosen", "ohnomyrosen", "someonecallanambulance"],
    "Daniel Naroditsky": ["SenseiDanya", "OhMyLands", "danielnaroditsky", "FrankfurtAirport"],
    "John Bartholomew": ["Fins0905"],
    "GingerGM": ["Ginger_GM"],
}
# lichess accounts, searched via the per-user export. That endpoint needs a
# personal API token (no scopes) - anonymous requests are either refused or
# held to one at a time per IP. Read from $LICHESS_TOKEN or scanner/.lichess_token.
LICHESS_ACCOUNTS = {
    "Eric Rosen": ["EricRosen"],
}
LICHESS_TOKEN_FILE = ROOT / "scanner" / ".lichess_token"
# How far before a video's publish date its games may have been played.
LOOKBACK_DAYS = 120

# Matching thresholds (plies of shared opening prefix).
MIN_PREFIX = 10          # archive match: a real game shares at least this much
MIN_PREFIX_SHORT = 6     # ...or the whole (short) reconstruction, if at least this long
MIN_PREFIX_LINKED = 4    # a game the video itself links needs less evidence
# ...and for an archive match, this share of the reconstruction's moves must
# line up with the real game overall. A different game sharing a common
# opening diverges completely afterwards (measured: 0.21-0.56), while a real
# match with a misread move or two stays high (>= 0.69).
MIN_ALIGNED = 0.65

# What counts as "correct" for each reading.
CLOCK_TOLERANCE_S = 2        # a clock value within this many seconds of %clk
CLOCK_GAME_OK_FRAC = 0.95    # a game's clocks are right if this share of values are
RATING_TOLERANCE = 20        # archives store the post-game rating; the screen shows pre-game

UA = "instructive-chess scanner benchmark (personal project)"


# --- fetching ---------------------------------------------------------------

_last_request: dict[str, float] = {}
MIN_GAP_S = 2.0   # between requests to the same host - be polite with the APIs (and the user's token)


def _get(url: str, headers: dict | None = None) -> bytes:
    """One request at a time, spaced out per host. On 429, waits a full
    minute before a single retry (lichess's documented rule), then gives up."""
    host = urllib.parse.urlsplit(url).netloc
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    for attempt in range(2):
        wait = _last_request.get(host, 0) + MIN_GAP_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request[host] = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                print(f"  (rate limited by {host}; waiting 60s)", file=sys.stderr)
                time.sleep(60)
                continue
            raise
    raise RuntimeError("unreachable")


def _cached_json(name: str, url: str, cacheable: bool = True):
    path = TRUTH_CACHE / name
    if path.exists():
        return json.loads(path.read_text())
    data = json.loads(_get(url))
    if cacheable:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    return data


@functools.lru_cache(maxsize=None)
def chesscom_month(user: str, year: int, month: int) -> list[dict]:
    today = date.today()
    finished = (year, month) < (today.year, today.month)
    try:
        data = _cached_json(f"chesscom_{user.lower()}_{year:04d}-{month:02d}.json",
                            f"https://api.chess.com/pub/player/{user.lower()}/games/{year:04d}/{month:02d}",
                            cacheable=finished)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise
    out = []
    for g in data.get("games", []):
        if g.get("rules") != "chess" or "pgn" not in g:
            continue
        rec = parse_pgn(g["pgn"])
        if rec is None:
            continue
        rec["source"] = g.get("url", f"chess.com:{user}")
        rec["end_time"] = g.get("end_time")
        out.append(rec)
    return out


def _lichess_token() -> str | None:
    token = os.environ.get("LICHESS_TOKEN")
    if not token and LICHESS_TOKEN_FILE.exists():
        token = LICHESS_TOKEN_FILE.read_text().strip()
    return token or None


@functools.lru_cache(maxsize=None)
def lichess_month(user: str, year: int, month: int) -> list[dict]:
    """One calendar month of *user*'s standard blitz/rapid/classical lichess
    games, with clocks. Needs a token; without one returns nothing."""
    path = TRUTH_CACHE / f"lichess_{user.lower()}_{year:04d}-{month:02d}.pgn"
    if path.exists():
        text = path.read_text()
    else:
        token = _lichess_token()
        if token is None:
            return []
        since = datetime(year, month, 1, tzinfo=timezone.utc)
        until = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=timezone.utc)
        url = (f"https://lichess.org/api/games/user/{user}?clocks=true&evals=false&opening=false"
               f"&perfType=blitz,rapid,classical&since={int(since.timestamp() * 1000)}"
               f"&until={int(until.timestamp() * 1000)}")
        text = _get(url, {"Authorization": f"Bearer {token}",
                          "Accept": "application/x-chess-pgn"}).decode()
        today = date.today()
        if (year, month) < (today.year, today.month):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    out = []
    stream = io.StringIO(text)
    while True:
        game = chess.pgn.read_game(stream)
        if game is None:
            break
        rec = parse_game(game)
        if rec is None:
            continue
        rec["source"] = game.headers.get("Site", f"lichess:{user}")
        try:
            played = datetime.strptime(f"{game.headers['UTCDate']} {game.headers['UTCTime']}",
                                       "%Y.%m.%d %H:%M:%S").replace(tzinfo=timezone.utc)
            rec["end_time"] = int(played.timestamp())
        except (KeyError, ValueError):
            rec["end_time"] = None
        out.append(rec)
    return out


def lichess_game(game_id: str) -> dict | None:
    path = TRUTH_CACHE / f"lichess_{game_id}.pgn"
    if not path.exists():
        text = _get(f"https://lichess.org/game/export/{game_id}?clocks=true&evals=false").decode()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    rec = parse_pgn(path.read_text())
    if rec:
        rec["source"] = f"https://lichess.org/{game_id}"
        rec["linked"] = True
    return rec


def chesscom_linked_game(game_id: str) -> dict | None:
    """A chess.com game link only carries an id - look up who played it and
    when, then pull the full PGN (with clocks) out of that player's archive."""
    meta = _cached_json(f"chesscom_live_{game_id}.json",
                        f"https://www.chess.com/callback/live/game/{game_id}")
    headers = meta["game"]["pgnHeaders"]
    y, m, _ = (int(x) for x in headers["Date"].split("."))
    for user in (headers["White"], headers["Black"]):
        for rec in chesscom_month(user, y, m):
            if rec["source"].rstrip("/").endswith(f"/{game_id}"):
                return dict(rec, linked=True)  # the month's records are shared (cached)
    return None


LINK_RE = re.compile(r"https?://(?:www\.)?(?:lichess\.org/([A-Za-z0-9]{8})(?:[A-Za-z0-9]{4})?\b"
                     r"|chess\.com/game/live/(\d+))")


def linked_games(video_id: str) -> list[dict]:
    info = VIDEO_CACHE / f"{video_id}.info.json"
    if not info.exists():
        return []
    desc = json.loads(info.read_text()).get("description", "")
    out = []
    for lichess_id, chesscom_id in LINK_RE.findall(desc):
        try:
            rec = lichess_game(lichess_id) if lichess_id else chesscom_linked_game(chesscom_id)
        except Exception as e:  # a dead link shouldn't sink the whole benchmark
            print(f"  ! {video_id}: couldn't fetch linked game {lichess_id or chesscom_id}: {e}",
                  file=sys.stderr)
            continue
        if rec:
            out.append(rec)
    return out


# --- parsing ----------------------------------------------------------------

def parse_pgn(text: str) -> dict | None:
    return parse_game(chess.pgn.read_game(io.StringIO(text)))


def parse_game(game: chess.pgn.Game | None) -> dict | None:
    if game is None or game.headers.get("Variant", "Standard") not in ("Standard", "Chess"):
        return None
    if game.headers.get("SetUp") == "1" and game.headers.get("FEN", chess.STARTING_FEN) != chess.STARTING_FEN:
        return None
    moves, clocks = [], []
    board = game.board()
    for node in game.mainline():
        moves.append(board.san(node.move))
        board.push(node.move)
        clocks.append(node.clock())
    h = game.headers

    def elo(key):
        try:
            return int(h.get(key, ""))
        except ValueError:
            return None

    return {
        "white": h.get("White"), "black": h.get("Black"),
        "white_elo": elo("WhiteElo"), "black_elo": elo("BlackElo"),
        "date": h.get("UTCDate") or h.get("Date"),
        "time_control": h.get("TimeControl"),
        "moves_san": moves, "clocks": clocks,
    }


# --- matching ---------------------------------------------------------------

def _prefix(a: list[str], b: list[str], ambiguous: list[int] = ()) -> int:
    """Plies of *a* that match *b* from the start. A ply in *ambiguous* (a
    route guessed among transpositions, see reconstruct.Game.ambiguous)
    counts as matching if the *position* agrees again by the end of its
    run - the moves' order was never observable, the position was."""
    n = 0
    amb = set(ambiguous)
    ba, bb = chess.Board(), chess.Board()
    for i, (x, y) in enumerate(zip(a, b)):
        try:
            ba.push_san(x)
            bb.push_san(y)
        except ValueError:
            break
        if x != y:
            if i not in amb:
                break
            # inside a guessed run: fine as long as the run ends on the real position
            j = i
            while j + 1 < min(len(a), len(b)) and j + 1 in amb:
                j += 1
            if j + 1 < min(len(a), len(b)) and a[j + 1] != b[j + 1]:
                break          # the run ends but the games still differ
            ba2, bb2 = ba.copy(), bb.copy()
            try:
                for k in range(i + 1, j + 1):
                    ba2.push_san(a[k])
                    bb2.push_san(b[k])
            except ValueError:
                break
            if ba2.board_fen() != bb2.board_fen() or ba2.turn != bb2.turn:
                break
        n += 1
    return n


def candidates_for_video(channel: str, published: str | None, video_id: str) -> list[dict]:
    cands = linked_games(video_id)
    if not published:
        return cands
    pub = date.fromisoformat(published)
    lo = pub - timedelta(days=LOOKBACK_DAYS)
    months = set()
    d = lo
    while d <= pub:
        months.add((d.year, d.month))
        d += timedelta(days=28)
    months.add((pub.year, pub.month))
    sources = ([(chesscom_month, u) for u in ACCOUNTS.get(channel, [])]
               + [(lichess_month, u) for u in LICHESS_ACCOUNTS.get(channel, [])])
    for fetch, user in sources:
        for y, m in sorted(months):
            for rec in fetch(user, y, m):
                end = rec.get("end_time")
                if end is None:
                    continue
                played = datetime.fromtimestamp(end, tz=timezone.utc).date()
                if lo <= played <= pub + timedelta(days=1):
                    cands.append(rec)
    return cands


def _aligned(a: list[str], b: list[str]) -> int:
    """Moves of *a* that line up with *b* in order - tolerant of a dropped or
    misread move, unlike a strict prefix."""
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return sum(block.size for block in sm.get_matching_blocks())


def match(game: dict, cands: list[dict]) -> tuple[dict | None, str]:
    moves = game["moves_san"]
    amb = game.get("ambiguous_plies", ())
    scored = sorted(((_prefix(moves, c["moves_san"], amb), c.get("linked", False), c) for c in cands),
                    key=lambda t: (t[0], t[1]), reverse=True)
    if not scored:
        return None, "no candidate games"
    best_n, best_linked, best = scored[0]
    need = MIN_PREFIX_LINKED if best_linked else min(MIN_PREFIX, max(MIN_PREFIX_SHORT, len(moves)))
    if best_n < need:
        return None, f"best shared prefix only {best_n} ply"
    if not best_linked and _aligned(moves, best["moves_san"]) < MIN_ALIGNED * len(moves):
        return None, "shares only the opening with its best candidate"
    # Ambiguous if a *different* game shares just as much (the same game can
    # appear twice - once linked, once from an archive).
    for n, _, c in scored[1:]:
        if n < best_n:
            break
        if c["moves_san"] != best["moves_san"]:
            if best_linked:
                break
            return None, f"ambiguous: {best_n}-ply prefix shared by several games"
    return best, "ok"


# --- scoring ----------------------------------------------------------------

def narrator_side(channel: str, real: dict) -> str | None:
    """Which colour the channel's own account played in the real game, or
    None when neither player is a known account (e.g. a linked game under an
    unknown alt). Checks the catalog's `pov` field (see catalog.tag)."""
    names = {u.lower() for u in ACCOUNTS.get(channel, []) + LICHESS_ACCOUNTS.get(channel, [])}
    if (real.get("white") or "").lower() in names:
        return "white"
    if (real.get("black") or "").lower() in names:
        return "black"
    return None


def score(game: dict, real: dict) -> dict:
    moves = game["moves_san"]
    prefix = _prefix(moves, real["moves_san"], game.get("ambiguous_plies", ()))
    trusted = game.get("trusted_ply", len(moves))
    res = {
        "trusted_ply": trusted,
        "trusted_ok": prefix >= trusted,
        "moves_correct": prefix,
        "ply": len(moves),
        "real_ply": len(real["moves_san"]),
        "moves_aligned": _aligned(moves, real["moves_san"]),
        "moves_ok": prefix == len(moves),
    }

    # Clocks: at each ply the mover's clock should read that move's %clk.
    # Only scored over the correctly reconstructed prefix - past a wrong
    # move there's no real ply to compare against.
    checked = within = missing = 0
    worst = None
    have_clocks = any(c is not None for c in real["clocks"])
    for i in range(prefix if have_clocks else 0):
        side = "white" if i % 2 == 0 else "black"
        truth = real["clocks"][i]
        if truth is None:
            continue
        checked += 1
        vals = game.get(f"clock_{side}_seconds") or []
        got = vals[i] if i < len(vals) else None
        if got is None:
            missing += 1
            continue
        err = abs(got - truth)
        if err <= CLOCK_TOLERANCE_S:
            within += 1
        elif worst is None or err > worst[1]:
            worst = (i, err, got, truth)
    res["clock"] = {
        "checked": checked, "within": within, "missing": missing,
        "wrong": checked - within - missing,
        "ok": checked > 0 and within >= CLOCK_GAME_OK_FRAC * checked,
        "worst": worst,
    }

    ratings = {}
    for side in ("white", "black"):
        truth, got = real[f"{side}_elo"], game.get(f"rating_{side}")
        if truth is None:
            status = "no truth"
        elif got is None:
            status = "missing"
        elif got == truth:
            status = "exact"
        elif abs(got - truth) <= RATING_TOLERANCE:
            status = "close"
        else:
            status = "wrong"
        ratings[side] = {"got": got, "truth": truth, "status": status}
    res["rating"] = ratings
    res["rating_ok"] = all(r["status"] in ("exact", "close") for r in ratings.values())
    return res


# --- report -----------------------------------------------------------------

def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:5.1f}%  ({n}/{d})" if d else "   n/a"


def run(catalog: Path) -> dict:
    games = json.loads(catalog.read_text())
    by_video: dict[str, list[dict]] = {}
    for g in games:
        by_video.setdefault(g["video_id"], []).append(g)

    rows = []
    for vid, vgames in by_video.items():
        g0 = vgames[0]
        try:
            cands = candidates_for_video(g0["channel"], g0.get("published"), vid)
        except Exception as e:
            print(f"  ! {vid}: couldn't load candidate games: {e}", file=sys.stderr)
            cands = []
        for g in vgames:
            real, why = match(g, cands)
            row = {"id": g["id"], "video_id": vid, "channel": g["channel"],
                   "title": g.get("video_title", ""), "start_seconds": g.get("start_seconds"),
                   "matched": real is not None, "why": why}
            if real is not None:
                row["truth_source"] = real["source"]
                row["platform"] = "lichess" if "lichess" in real["source"] else "chess.com"
                row["linked"] = real.get("linked", False)
                row.update(score(g, real))
                row["pov_truth"] = narrator_side(g0["channel"], real)
                row["pov_ok"] = (g.get("pov") == row["pov_truth"]) if row["pov_truth"] and g.get("pov") else None
            rows.append(row)
    return {"rows": rows, "n_catalog": len(games)}


def _summary(m: list[dict], indent: str = "  ") -> None:
    """The per-game correctness lines (the 95% target) for one group of games."""
    print(f"{indent}Moves    every move right       {_pct(sum(r['moves_ok'] for r in m), len(m))}")
    print(f"{indent}Clocks   ≥{CLOCK_GAME_OK_FRAC:.0%} of values ±{CLOCK_TOLERANCE_S}s     "
          f"{_pct(sum(r['clock']['ok'] for r in m), len(m))}")
    print(f"{indent}Ratings  both sides ±{RATING_TOLERANCE}         {_pct(sum(r['rating_ok'] for r in m), len(m))}")


def print_report(result: dict, show_all: bool) -> None:
    rows = result["rows"]
    m = [r for r in rows if r["matched"]]
    print(f"Catalog: {result['n_catalog']} games · matched to a real game: {_pct(len(m), len(rows))}")
    unmatched: dict[str, int] = {}
    for r in rows:
        if not r["matched"]:
            key = re.sub(r"\d+", "N", r["why"])
            unmatched[key] = unmatched.get(key, 0) + 1
    for why, n in sorted(unmatched.items(), key=lambda kv: -kv[1]):
        print(f"    unmatched - {why}: {n}")
    if not m:
        return

    print("\nPer-game correctness (the 95% target), over matched games:")
    _summary(m)
    for platform in ("lichess", "chess.com"):
        sub = [r for r in m if r["platform"] == platform]
        if sub:
            print(f"  -- {platform} only ({len(sub)} games)")
            _summary(sub, indent="     ")

    pov = [r for r in m if r.get("pov_ok") is not None]
    print(f"  Narrator's colour detected right   {_pct(sum(r['pov_ok'] for r in pov), len(pov))}")
    wrong_pov = [r["id"] for r in pov if not r["pov_ok"]]
    if wrong_pov:
        print(f"           wrong: {' '.join(wrong_pov)}")

    tp = sum(r["trusted_ply"] for r in m)
    print("\nTrusted moves (what the web app shows - see scanner/trust.py):")
    print(f"  games with a wrong trusted move   {_pct(sum(not r['trusted_ok'] for r in m), len(m))}")
    print(f"  trusted plies that are wrong      "
          f"{_pct(sum(max(0, r['trusted_ply'] - r['moves_correct']) for r in m), tp)}")
    print(f"  plies trusted, of those recorded  {_pct(tp, sum(r['ply'] for r in m))}")
    print(f"  whole 15-move opening trusted     "
          f"{_pct(sum(r['trusted_ply'] >= min(30, r['real_ply']) for r in m), len(m))}")

    tot = sum(r["ply"] for r in m)
    ck = sum(r["clock"]["checked"] for r in m)
    print("\nDetail:")
    print(f"  Moves    plies before first error     {_pct(sum(r['moves_correct'] for r in m), tot)}")
    print(f"           plies aligned with real game {_pct(sum(r['moves_aligned'] for r in m), tot)}")
    print(f"           plies captured of real game  {_pct(tot, sum(r['real_ply'] for r in m))}")
    print(f"  Clocks   values within ±{CLOCK_TOLERANCE_S}s          {_pct(sum(r['clock']['within'] for r in m), ck)}")
    print(f"           missing                      {_pct(sum(r['clock']['missing'] for r in m), ck)}")
    print(f"           wrong                        {_pct(sum(r['clock']['wrong'] for r in m), ck)}")
    sides = [s for r in m for s in r["rating"].values() if s["status"] != "no truth"]
    for status in ("exact", "close", "missing", "wrong"):
        label = "Ratings  " if status == "exact" else "         "
        print(f"  {label}{status:<28} {_pct(sum(s['status'] == status for s in sides), len(sides))}")

    def badness(r):
        return (not r["moves_ok"]) * 4 + (not r["clock"]["ok"]) * 2 + (not r["rating_ok"])

    shown = m if show_all else [r for r in m if badness(r)]
    shown.sort(key=lambda r: (-badness(r), r["id"]))
    if not show_all:
        shown = shown[:25]
    if shown:
        print(f"\n{'All' if show_all else 'Worst'} matched games:")
        for r in shown:
            c, rt = r["clock"], r["rating"]
            clock = f"clk {c['within']}/{c['checked']}" + (f" miss {c['missing']}" if c["missing"] else "")
            if c["worst"]:
                i, err, got, truth = c["worst"]
                clock += f" (ply {i + 1}: {got} vs {truth:.0f})"
            rating = " ".join(f"{s[0]}:{v['got']}/{v['truth']}" for s, v in rt.items()
                              if v["status"] not in ("exact", "close"))
            plat = "L" if r["platform"] == "lichess" else "C"
            print(f"  {plat} {r['id']:<16} moves {r['moves_correct']}/{r['ply']} (real {r['real_ply']})  "
                  f"{clock}  {('rating ' + rating) if rating else ''}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalog", type=Path, default=CATALOG)
    ap.add_argument("--all", action="store_true", help="list every matched game, not just the worst")
    ap.add_argument("--json", type=Path, help="also write per-game results here")
    args = ap.parse_args(argv)
    result = run(args.catalog)
    print_report(result, args.all)
    if args.json:
        args.json.write_text(json.dumps(result, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
