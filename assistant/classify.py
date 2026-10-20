"""Sort each unit of commentary (units.py) into instructive, move-calling or
chatter with a cheap model, and restate the instructive ones as short ideas.

    ZAI_API_KEY=... scanner/.venv/bin/python assistant/classify.py VIDEO_ID [VIDEO_ID ...]
        [--model glm-5.3-flash] [--catalog PATH] [--force]

One request per BATCH consecutive units of a game (WORKERS at once), so the model reads the
commentary in order (captions cut sentences across units). Labels are saved
per video in assistant/cache/labels/<video_id>.json with the token counts.
Only the units' text and moves go to the API. The User-Agent is
$ASSISTANT_USER_AGENT, or "whittle".
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import positions
import units

LABELS = positions.ROOT / "assistant" / "cache" / "labels"
API = "https://api.z.ai/api/paas/v4/chat/completions"
BATCH = 12
WORKERS = int(os.environ.get("ASSISTANT_WORKERS", "2"))  # requests in flight (z.ai rate-limits more)
USER_AGENT = os.environ.get("ASSISTANT_USER_AGENT", "whittle")

PROMPT = """You are labelling the live commentary of a chess player (a master) narrating their own online games.
The captions are auto-generated, so chess notation is often garbled ("night f3" = Nf3, "bishop takes" = Bx..., "queen d2" = Qd2).
Each unit is what was said while one position was on the board; "moves" are the last few moves played (from the scanner) to help you decode notation.
Sentences run across units, so read them in order.

Label every unit with "kind":
- "instructive": says something a student could learn about chess: a plan, the idea or reason behind a move, a threat or tactic and why it works, an evaluation WITH a reason, pawn structure, piece placement, opening theory or typical ideas, endgame technique.
- "move": calls or describes moves, or gives a bare evaluation or reaction with no reason ("I'll take", "queen d2", "this is winning", "oh no").
- "chatter": not about the position on the board (chat, standings, the stream, life, jokes, sponsors).
If a unit mixes kinds, choose the most useful one (instructive > move > chatter).

For "instructive" units only, also give:
- "topics": a list from plan, reason, tactic, evaluation, structure, opening, endgame
- "idea": the point in one short sentence of clean chess English with correct SAN notation, standing on its own (e.g. "In this London structure White plays h4 to gain space before Black castles."). Do not invent anything that was not said.

Reply with JSON only: {"labels": [{"id": "...", "kind": "...", "topics": [...], "idea": "..."}, ...]} with one entry per unit, in order."""


def ask(model: str, payload: list[dict], key: str) -> tuple[list[dict], dict]:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": PROMPT},
                     {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        "temperature": 0.1,
        "thinking": {"type": "enabled", "effort": "low"},  # this model always thinks
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request(API, body, {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                       "User-Agent": USER_AGENT})
    for attempt in range(8):
        try:
            with urllib.request.urlopen(req, timeout=400) as r:
                res = json.loads(r.read())
            text = res["choices"][0]["message"]["content"]
            text = text[text.find("{"):text.rfind("}") + 1]
            return json.loads(text)["labels"], res.get("usage", {})
        except (urllib.error.URLError, json.JSONDecodeError, KeyError, TimeoutError) as e:
            err = e.read().decode()[:300] if isinstance(e, urllib.error.HTTPError) else str(e)
            if isinstance(e, urllib.error.HTTPError) and e.code < 500 and e.code != 429:
                raise RuntimeError(err) from e
            print(f"   retry {attempt + 1}: {err}", file=sys.stderr)
            time.sleep(min(120, 5 * 2 ** attempt))
    raise RuntimeError("API failed 8 times")


def classify_video(video_id: str, catalog: list[dict], model: str, key: str) -> dict | None:
    us = units.video_units(video_id, catalog)
    if us is None:
        return None
    labels: dict[str, dict] = {}
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "requests": 0}
    by_game: dict[str, list[units.Unit]] = {}
    for u in us:
        by_game.setdefault(u.game_id, []).append(u)
    chunks = [gus[i:i + BATCH] for gus in by_game.values() for i in range(0, len(gus), BATCH)]

    def run(chunk: list[units.Unit]) -> tuple[list[units.Unit], list[dict], dict]:
        payload = [{"id": u.id, "moves": u.recent, "said": u.text} for u in chunk]
        return (chunk, *ask(model, payload, key))

    with ThreadPoolExecutor(WORKERS) as pool:
        for chunk, got, use in pool.map(run, chunks):
            usage["prompt_tokens"] += use.get("prompt_tokens", 0)
            usage["completion_tokens"] += use.get("completion_tokens", 0)
            usage["requests"] += 1
            ids = {u.id for u in chunk}
            for g in got:
                if isinstance(g, dict) and g.get("id") in ids:
                    labels[g["id"]] = {k: g[k] for k in ("kind", "topics", "idea") if k in g}
    return {"video_id": video_id, "model": model, "usage": usage,
            "units": [asdict(u) | {"label": labels.get(u.id)} for u in us]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video_ids", nargs="+")
    ap.add_argument("--model", default="glm-5.3-flash")
    ap.add_argument("--catalog", type=Path, default=positions.CATALOG)
    ap.add_argument("--force", action="store_true", help="relabel videos already labelled")
    args = ap.parse_args(argv)
    key = os.environ.get("ZAI_API_KEY")
    if not key:
        print("ZAI_API_KEY is not set", file=sys.stderr)
        return 2
    catalog = json.loads(args.catalog.read_text())
    LABELS.mkdir(parents=True, exist_ok=True)
    for v in args.video_ids:
        out = LABELS / f"{v}.json"
        if out.exists() and not args.force:
            continue
        t = time.time()
        res = classify_video(v, catalog, args.model, key)
        if res is None:
            print(f"{v}: no transcript")
            continue
        out.write_text(json.dumps(res, ensure_ascii=False))
        n = len(res["units"])
        missing = sum(u["label"] is None for u in res["units"])
        u = res["usage"]
        print(f"{v}: {n} units ({missing} unlabelled), {u['requests']} requests, "
              f"{u['prompt_tokens']} in / {u['completion_tokens']} out tokens, {time.time() - t:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
