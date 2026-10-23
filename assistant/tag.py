"""A second, cheap pass over the labelled ideas (classify.py): which are advice
on what to play rather than about the line on the board, and where advice
belongs.

    scanner/.venv/bin/python assistant/tag.py [--only NAME] [--model claude-haiku-5-5]

An idea is pinned to the position on the board when it was said. That suits
an idea about the line, but advice is about a choice: "against the Sicilian,
play the Grand Prix or 2.c3", said after 1.e4 c5 2.c3, belongs after 1.e4 c5,
where the choice is made. For each idea the model says whether it is advice,
and if so which move of the line it is about choosing and what it recommends
there. Writes assistant/cache/tags.json ({unit id: tag}); ideas already
tagged are skipped.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import anthropic

import distill
import positions

TAGS = distill.CACHE / "tags.json"
BATCH = 20
WORKERS = 4

PROMPT = """You are sorting ideas that chess masters said aloud while playing online games. Each idea has an id, the game's moves up to the moment it was said ("line"), the idea, and the words it came from ("said").

For each idea, decide "advice": true only if it is advice on which OPENING or LINE to adopt, the kind of thing that goes in a repertoire: recommending an opening, a variation or a setup ("I recommend the Grand Prix or 2.c3 against the Sicilian"), saying which openings suit which players ("good for beginners", "if you like sharp games"), naming one's own repertoire choice, or preferring one established line over another ("the main alternative is 6.dxe5, but I prefer the old-school 6.Be2").
It is false for anything about the position on the board, even when it says what a player should or should not do: plans, reasons for a move, threats, evaluations, structures, which piece to trade or where to castle ("Black should not trade pieces in this IQP position", "Black should take with cxd4 because otherwise...", "White plays d5 to keep the center closed"). It is false for opening theory stated as fact ("the main line is 5.Nf3", "4.c4 is bad because of 4...e6"), and for naming or describing what is being played ("Black plays 2...b6, an offbeat line", "White plays the c3 Sicilian").

For advice only, also give:
- "choice": the move of the line the advice is about choosing, written as it appears in the line with its number ("2.c3", "3...d5"). Use the move where the alternatives branch: advice to play "the Grand Prix or 2.c3 against the Sicilian" is about choosing White's second move, so "2.c3" even if more moves have been played since. Use null if the advice is about a move not yet played (the next move from the current position).
- "options": the moves or lines the advice recommends at that choice, as short SAN with move numbers ("2.Nc3 and 3.f4", "2.c3"), or as named in the idea if no moves are given ("the Grand Prix Attack"). Empty if it only advises against something.

Reply with JSON only: {"tags": [{"id": "...", "advice": true|false, "choice": "..."|null, "options": [...]}, ...]}, one entry per idea, in order."""

SCHEMA = {
    "type": "object",
    "properties": {"tags": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "advice": {"type": "boolean"},
            "choice": {"type": ["string", "null"]},
            "options": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["id", "advice"],
        "additionalProperties": False,
    }}},
    "required": ["tags"],
    "additionalProperties": False,
}
MOVE = re.compile(r"(\d+)(\.\.\.|\.)\s*([^\s.]+?)[+#!?]*(?=\s|$)")


def choice_ply(choice: str | None, line: list[str], ply: int) -> int:
    """The plies before the move *choice* names in *line*: the position where
    the choice is made. Advice about the next move, or a move that isn't in
    the line, stays where it was said."""
    found = MOVE.findall(choice or "")     # traced: the whole line, "1.e4 c5 2.c3", for its last move
    if not found:
        return ply
    number, dots, san = found[-1]
    i = 2 * (int(number) - 1) + (dots == "...")
    return i if i < ply and line[i].rstrip("+#") == san.rstrip("+#") else ply


def gather(prefixes: list[list[str]] | None) -> list[dict]:
    games = {g["id"]: g for g in json.loads(positions.CATALOG.read_text())}
    out = []
    for f in sorted(glob.glob(str(distill.CACHE / "labels" / "*.json"))):
        for u in json.loads(open(f).read())["units"]:
            lab = u.get("label") or {}
            g = games.get(u["game_id"])
            if g and lab.get("kind") == "instructive" and lab.get("evidence_ok") and lab.get("idea") \
                    and u["trusted"] and (prefixes is None or distill._matches(g["moves_san"], prefixes)):
                out.append({"id": u["id"], "line": g["moves_san"][:u["ply"]], "idea": lab["idea"],
                            "said": lab.get("evidence", "")})
    return out


def ask(client, model: str, batch: list[dict]) -> tuple[list[dict], dict]:
    payload = [{"id": b["id"], "line": positions.line_san(b["line"]) or "(start)", "idea": b["idea"],
                "said": b["said"]} for b in batch]
    use = {"input_tokens": 0, "output_tokens": 0}
    for _ in range(3):
        res = client.messages.create(
            model=model, max_tokens=4000, system=PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        )
        use["input_tokens"] += res.usage.input_tokens
        use["output_tokens"] += res.usage.output_tokens
        try:
            return json.loads(next(b.text for b in res.content if b.type == "text"))["tags"], use
        except (StopIteration, json.JSONDecodeError, KeyError):
            print(f"   unreadable reply ({res.stop_reason}), retrying", file=sys.stderr)
    return [], use


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="tag only the ideas from this opening in distill.OPENINGS")
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--force", action="store_true", help="tag again ideas already tagged")
    args = ap.parse_args(argv)
    key = os.environ.get("HAIKU_API_KEY", "")
    if not key:
        print("HAIKU_API_KEY is not set", file=sys.stderr)
        return 2
    tags = json.loads(TAGS.read_text()) if TAGS.exists() else {}
    ideas = [i for i in gather(distill.OPENINGS[args.only] if args.only else None) if args.force or i["id"] not in tags]
    by_id = {i["id"]: i for i in ideas}
    client = anthropic.Anthropic(api_key=key, max_retries=6)
    batches = [ideas[i:i + BATCH] for i in range(0, len(ideas), BATCH)]
    use = {"input_tokens": 0, "output_tokens": 0}
    with ThreadPoolExecutor(WORKERS) as pool:
        for got, u in pool.map(lambda b: ask(client, args.model, b), batches):
            use = {k: use[k] + u[k] for k in use}
            for t in got:
                i = by_id.get(t.get("id"))
                if i is None:
                    continue
                tag = {"advice": bool(t.get("advice")), "model": args.model}
                if tag["advice"]:
                    tag["choice"] = t.get("choice")
                    tag["at_ply"] = choice_ply(t.get("choice"), i["line"], len(i["line"]))
                    tag["options"] = t.get("options") or []
                tags[i["id"]] = tag
    TAGS.write_text(json.dumps(tags, indent=1, ensure_ascii=False))
    done = sum(i["id"] in tags for i in ideas)
    print(f"{done}/{len(ideas)} ideas tagged, {sum(tags[i['id']]['advice'] for i in ideas if i['id'] in tags)} advice; "
          f"{use['input_tokens']} in / {use['output_tokens']} out tokens")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
