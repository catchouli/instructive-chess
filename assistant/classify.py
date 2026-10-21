"""Sort each unit of commentary (units.py) into instructive, move-calling or
chatter with a cheap model, and restate the instructive ones as short ideas.

    scanner/.venv/bin/python assistant/classify.py VIDEO_ID [VIDEO_ID ...]
        [--model claude-haiku-5-5] [--catalog PATH] [--force]

Claude models need ANTHROPIC_API_KEY, z.ai's GLM models (--model glm-5.3-flash) ZAI_API_KEY.

One request per BATCH consecutive units of a game (WORKERS at once), so the model reads the
commentary in order (captions cut sentences across units). Labels are saved
per video in assistant/cache/labels/<video_id>.json with the token counts.
Only the units' text and moves go to the API. The User-Agent for z.ai is
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

import anthropic

import positions
import units

LABELS = positions.ROOT / "assistant" / "cache" / "labels"
API = "https://api.z.ai/api/paas/v4/chat/completions"
BATCH = 12
WORKERS = int(os.environ.get("ASSISTANT_WORKERS", "4"))  # requests in flight (z.ai rate-limits at 6, use 2)
USER_AGENT = os.environ.get("ASSISTANT_USER_AGENT", "whittle")

PROMPT = """You are labelling the live commentary of a chess player (a master) narrating their own online games.
The captions are auto-generated, so chess notation is often garbled ("night f3" = Nf3, "bishop takes" = Bx..., "queen d2" = Qd2).
Each unit is what was said while one position was on the board; "moves" are the last few moves played (from the scanner) to help you decode notation.
Sentences run across units, so read them in order.

Label every unit with "kind":
- "instructive": says something a student could learn about chess: a plan or goal for the position, the idea or reason behind a move, a threat or tactic and why it works, an evaluation WITH a reason, pawn structure, piece placement, opening theory or typical ideas (including which line is the main line), endgame technique.
- "move": calls or describes moves, or gives a bare evaluation or reaction with no reason ("I'll take", "queen d2", "this is winning", "oh no", "knight f5 is playable", "this is risky", "I hope he plays it out"). Naming a move as good, bad, correct or playable without saying why is "move". So is listing candidate moves, or asking which move to play, without a reason.
- "chatter": not about the position on the board (chat, standings, the stream, life, jokes, sponsors, remarks about the clock or time left, greetings, insults), or a fragment too short to be about anything ("but", "and this is").
If a unit mixes kinds, choose the most useful one (instructive > move > chatter).

The test for "instructive" is whether the unit itself states a reason, an idea or a plan, usually with words like because, so that, to, the idea is, the plan is, it comes with, it stops, it takes away, the main line is.
Examples:
- "rook e1 because it stops bishop f5" -> instructive (reason)
- "I want to get my knight to d5 at some point" -> instructive (plan)
- "this comes with tempo on the queen" -> instructive (reason)
- "bishop g5 is the main line here" -> instructive (opening)
- "bishop g5 is a good move" -> move (no reason)
- "I'm happy with this" / "looking okay" / "this gives me hope" -> move (bare evaluation)
- "I have 30 seconds left, need to hurry" -> chatter (the clock)
A reason that the captions cut off before it is said ("I'm giving back a pawn but I'm trying") is not instructive yet; if the next unit completes it, label the next unit instructive.

For "instructive" units only, also give:
- "evidence": the exact words from this unit's "said" that carry the reason, idea or plan, copied verbatim (garbled spelling included), as short as will do.
- "topics": a list from plan, reason, tactic, evaluation, structure, opening, endgame
- "idea": what the evidence says, in one short sentence of clean chess English with correct SAN notation, standing on its own (e.g. "White plays h4 to gain space before Black castles."). Only what the narrator said: decode the notation, but do not add reasons, plans or evaluations of your own, even correct ones. The "moves" are there to decode notation, never as a source of ideas. If the words are too vague for a clear idea, the unit is not instructive.

Reply with JSON only: {"labels": [{"id": "...", "kind": "...", "evidence": "...", "topics": [...], "idea": "..."}, ...]} with one entry per unit, in order."""


LABEL_SCHEMA = {
    "type": "object",
    "properties": {"labels": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "kind": {"type": "string", "enum": ["instructive", "move", "chatter"]},
            "topics": {"type": "array", "items": {"type": "string", "enum": [
                "plan", "reason", "tactic", "evaluation", "structure", "opening", "endgame"]}},
            "evidence": {"type": "string"},
            "idea": {"type": "string"},
        },
        "required": ["id", "kind"],
        "additionalProperties": False,
    }}},
    "required": ["labels"],
    "additionalProperties": False,
}
_claude = None


def ask(model: str, payload: list[dict], key: str) -> tuple[list[dict], dict]:
    """Labels for every unit in *payload*: a reply that leaves some out (traced:
    8 of 12, more often since the evidence field) is asked for again."""
    labels, use = [], {"prompt_tokens": 0, "completion_tokens": 0}
    for _ in range(3):
        have = {lab.get("id") for lab in labels}
        if all(p["id"] in have for p in payload):
            break
        got, u = ask_claude(model, payload) if model.startswith("claude") else ask_zai(model, payload, key)
        labels += [lab for lab in got if isinstance(lab, dict) and lab.get("id") not in have]
        use = {k: use[k] + u.get(k, 0) for k in use}
    return labels, use


def ask_claude(model: str, payload: list[dict]) -> tuple[list[dict], dict]:
    global _claude
    _claude = _claude or anthropic.Anthropic(max_retries=6)
    use = {"prompt_tokens": 0, "completion_tokens": 0}
    for effort in ("low", "medium", "medium"):
        res = _claude.messages.create(
            model=model, max_tokens=8000, system=PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": LABEL_SCHEMA}},
        )
        use["prompt_tokens"] += res.usage.input_tokens
        use["completion_tokens"] += res.usage.output_tokens
        text = next((b.text for b in res.content if b.type == "text"), "")
        try:
            return json.loads(text)["labels"], use
        except (json.JSONDecodeError, KeyError):
            # traced: "]}]}]}..." repeated until max_tokens, once in ~60 batches
            print(f"   unreadable reply ({res.stop_reason}, {res.usage.output_tokens} tokens), retrying",
                  file=sys.stderr)
    return [], use


def ask_zai(model: str, payload: list[dict], key: str) -> tuple[list[dict], dict]:
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


def _norm(text: str) -> str:
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in text).split())


def _found(quote: list[str], words: list[str], lo: int, hi: int) -> bool:
    """Do the quote's words appear in order in *words*, spread over at most twice
    the quote's length (a repeated phrase left out), touching words[lo:hi]?"""
    for start in range(len(words)):
        if words[start] != quote[0]:
            continue
        i, j = 0, start
        while i < len(quote) and j < len(words) and j - start < 2 * len(quote):
            if words[j] == quote[i]:
                i += 1
            j += 1
        if i == len(quote) and start < hi and j > lo:
            return True
    return False


def checked(label: dict, said: str, before: str = "", after: str = "") -> dict:
    """The label's fields, with `evidence_ok`: is the quoted evidence really in
    what was said? An idea without it may be the model's own (traced: an attack
    plan read off the moves, not the waffle said over them). The quote may run
    into the units either side (captions cut sentences across positions), but
    must touch this one."""
    out = {k: label[k] for k in ("kind", "evidence", "topics", "idea") if k in label}
    if out.get("kind") == "instructive":
        quote = _norm(out.get("evidence", "")).split()
        b, m, a = _norm(before).split(), _norm(said).split(), _norm(after).split()
        out["evidence_ok"] = bool(quote) and _found(quote, b + m + a, len(b), len(b) + len(m))
    return out


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
                    k = next(i for i, u in enumerate(chunk) if u.id == g["id"])
                    labels[g["id"]] = checked(g, chunk[k].text, chunk[k - 1].text if k else "",
                                              chunk[k + 1].text if k + 1 < len(chunk) else "")
    return {"video_id": video_id, "model": model, "usage": usage,
            "units": [asdict(u) | {"label": labels.get(u.id)} for u in us]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video_ids", nargs="+")
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--catalog", type=Path, default=positions.CATALOG)
    ap.add_argument("--force", action="store_true", help="relabel videos already labelled")
    args = ap.parse_args(argv)
    var = "ANTHROPIC_API_KEY" if args.model.startswith("claude") else "ZAI_API_KEY"
    key = os.environ.get(var, "")
    if not key:
        print(f"{var} is not set", file=sys.stderr)
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
