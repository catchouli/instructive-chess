"""Distil the instructive commentary on one opening into a short study guide,
every point cited to the moments it came from (prototype).

    scanner/.venv/bin/python assistant/distill.py [--model claude-sonnet-5-5] [--only NAME]

For each opening in OPENINGS, gathers the labelled ideas (classify.py) from
trusted plies (up to MAX_PLY) of games that start with its moves, with the
evidence quoted in the transcript, and asks the model for a study guide built
only from them. Writes assistant/cache/summaries.html (local: it links each
citation to the video moment, and ideas quote transcripts) and the raw replies
as assistant/cache/summaries/<name>.json.
"""
from __future__ import annotations

import argparse
import glob
import html
import json
import os
import re
import sys

import anthropic

import positions

CACHE = positions.ROOT / "assistant" / "cache"
OUT_DIR = CACHE / "summaries"
MAX_PLY = 30

OPENINGS = {
    "The London System": [["d4", "d5", "Bf4"], ["d4", "Nf6", "Bf4"]],
    "Sicilian Defence: Alapin (2.c3)": [["e4", "c5", "c3"]],
    "Caro-Kann Defence": [["e4", "c6"]],
}

PROMPT = """You are writing a short study guide on one chess opening for a club player, built ONLY from a list of ideas that strong players said aloud while playing it in YouTube videos. Each idea has an id, who said it, the moves leading to the position, and the idea itself.

Write in Markdown:
- A title line: "# <opening>".
- Sections with "## " headings that fit the material, for example: the main plans for each side, typical pawn structures, key moves and move orders, traps and tactics, common mistakes. Skip a section the ideas don't support.
- Short bullet points. Every bullet ends with the ids of the ideas it rests on, like [12] or [3, 41]. Merge ideas that say the same thing and cite them all: agreement between several players is worth showing. Where players disagree, say so.
- Use correct SAN notation, and name the position a point applies to (e.g. "after 3.Bf4 c5").
- Add nothing that isn't in the ideas, even if you know it to be true. Leave out ideas that only make sense in one specific game.
- About 300-600 words."""


def _matches(moves: list[str], prefixes: list[list[str]]) -> bool:
    return any(moves[:len(p)] == p for p in prefixes)


def gather(prefixes: list[list[str]]) -> list[dict]:
    games = {g["id"]: g for g in json.loads(positions.CATALOG.read_text())}
    ideas = []
    for f in sorted(glob.glob(str(CACHE / "labels" / "*.json"))):
        for u in json.loads(open(f).read())["units"]:
            lab = u.get("label") or {}
            g = games.get(u["game_id"])
            if not (g and lab.get("kind") == "instructive" and lab.get("evidence_ok") and u["trusted"]
                    and u["ply"] <= MAX_PLY and _matches(g["moves_san"], prefixes)):
                continue
            ideas.append({"id": len(ideas) + 1, "by": u["channel"], "moves": u["recent"] or "(start)",
                          "idea": lab["idea"], "video_id": u["video_id"], "start": u["start"],
                          "title": g.get("video_title", "")})
    return ideas


def distil(client, model: str, name: str, ideas: list[dict]) -> tuple[str, dict]:
    listing = "\n".join(f"[{i['id']}] ({i['by']}; after {i['moves']}) {i['idea']}" for i in ideas)
    res = client.messages.create(
        model=model, max_tokens=4000, system=PROMPT,
        messages=[{"role": "user", "content": f"Opening: {name}\n\nIdeas:\n{listing}"}],
        output_config={"effort": "medium"},
    )
    text = "".join(b.text for b in res.content if b.type == "text")
    return text, {"input_tokens": res.usage.input_tokens, "output_tokens": res.usage.output_tokens}


def to_html(md: str, ideas: list[dict]) -> str:
    """Just enough Markdown for the guide, with [n] citations linked to the moments."""
    by_id = {i["id"]: i for i in ideas}

    def cite(m):
        links = []
        for n in re.findall(r"\d+", m.group(1)):
            i = by_id.get(int(n))
            if i:
                t = max(0, int(i["start"]))
                tip = html.escape(f"{i['by']}, after {i['moves']}: {i['idea']}")
                links.append(f'<a href="https://www.youtube.com/watch?v={i["video_id"]}&t={t}s" '
                             f'target="_blank" title="{tip}">{n}</a>')
        return f'<sup>[{", ".join(links)}]</sup>' if links else m.group(0)

    out, in_list = [], False
    for line in md.splitlines():
        s = html.escape(line.strip())
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"\[([\d,\s]+)\]", cite, s)
        if s.startswith("- ") or s.startswith("* "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{s[2:]}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if s.startswith("## "):
            out.append(f"<h3>{s[3:]}</h3>")
        elif s.startswith("# "):
            out.append(f"<h2>{s[2:]}</h2>")
        elif s:
            out.append(f"<p>{s}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Opening guides</title>
<style>
:root{--bg:#fbfaf7;--ink:#1d1d1b;--mute:#6b6a65;--line:#e4e1d8;--acc:#3a6ea5}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--ink:#ecebe6;--mute:#9d9b93;--line:#33322d;--acc:#6ea8dc}}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 -apple-system,system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:24px 16px 60px}
h1{font-size:24px}h2{margin-top:44px;border-top:1px solid var(--line);padding-top:24px}
.meta{color:var(--mute);font-size:14px}sup a{color:var(--acc);text-decoration:none;font-size:11px}
sup a:hover{text-decoration:underline}li{margin:4px 0}
</style></head><body><main><h1>Opening guides (prototype)</h1>
<p class="meta">Written by __MODEL__ only from what the narrators said while playing these openings
(instructive moments with their words found in the transcript, on trusted moves, up to ply __MAXPLY__).
Each number links to that moment in the video; hover for the idea it rests on.</p>
__BODY__</main></body></html>"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--only", help="one opening, by (part of) its name")
    args = ap.parse_args(argv)
    client = anthropic.Anthropic(api_key=os.environ.get("HAIKU_API_KEY"), max_retries=6)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    body = []
    for name, prefixes in OPENINGS.items():
        if args.only and args.only.lower() not in name.lower():
            continue
        ideas = gather(prefixes)
        md, usage = distil(client, args.model, name, ideas)
        slug = re.sub(r"\W+", "_", name).strip("_").lower()
        (OUT_DIR / f"{slug}.json").write_text(json.dumps({"name": name, "model": args.model, "usage": usage,
                                                          "ideas": ideas, "guide": md}, ensure_ascii=False, indent=1))
        by = {}
        for i in ideas:
            by[i["by"]] = by.get(i["by"], 0) + 1
        body.append(to_html(md, ideas) + f'<p class="meta">From {len(ideas)} ideas: '
                    + ", ".join(f"{k} {v}" for k, v in sorted(by.items(), key=lambda kv: -kv[1])) + "</p>")
        print(f"{name}: {len(ideas)} ideas, {usage['input_tokens']} in / {usage['output_tokens']} out tokens")
    out = CACHE / "summaries.html"
    out.write_text(PAGE.replace("__MODEL__", args.model).replace("__MAXPLY__", str(MAX_PLY)).replace("__BODY__", "\n".join(body)))
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
