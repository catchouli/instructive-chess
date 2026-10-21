"""A hand-labelled sample of units (units.py) to check a model's labels against
(classify.py) before trusting it on the rest.

    scanner/.venv/bin/python assistant/validate.py sample [--n 80] [--seed 20261021]
    scanner/.venv/bin/python assistant/validate.py label [--model claude-haiku-5-5]
    scanner/.venv/bin/python assistant/validate.py score [--model claude-haiku-5-5]

`sample` draws random units from every video with a transcript into
assistant/cache/sample.jsonl. Label them by hand in sample_hand.json
({unit id: "instructive" | "move" | "chatter"}) *before* any model sees them.
`label` sends each sampled unit with the units around it, in the same batches
classify.py would send, and saves the model's labels; `score` compares them.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import random
import sys
from dataclasses import asdict
from pathlib import Path

import classify
import positions
import units

CACHE = positions.ROOT / "assistant" / "cache"
SAMPLE = CACHE / "sample.jsonl"
HAND = CACHE / "sample_hand.json"


def model_file(model: str) -> Path:
    return CACHE / f"sample_{model}.json"


def batches(catalog: list[dict]) -> dict[str, list[units.Unit]]:
    """Every unit's batch, as classify.py cuts them: BATCH consecutive units of a game."""
    out = {}
    for vid in sorted({e["video_id"] for e in catalog}):
        us = units.video_units(vid, catalog) or []
        by_game: dict[str, list[units.Unit]] = {}
        for u in us:
            by_game.setdefault(u.game_id, []).append(u)
        for gus in by_game.values():
            for i in range(0, len(gus), classify.BATCH):
                chunk = gus[i:i + classify.BATCH]
                for u in chunk:
                    out[u.id] = chunk
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["sample", "label", "score"])
    ap.add_argument("--n", type=int, default=80)
    ap.add_argument("--seed", type=int, default=20261021)
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--catalog", type=Path, default=positions.CATALOG)
    args = ap.parse_args(argv)
    CACHE.mkdir(parents=True, exist_ok=True)

    if args.command == "sample":
        chunks = batches(json.loads(args.catalog.read_text()))
        ids = random.Random(args.seed).sample(sorted(chunks), args.n)
        with SAMPLE.open("w") as f:
            for i in ids:
                u = next(u for u in chunks[i] if u.id == i)
                f.write(json.dumps(asdict(u) | {"batch": [asdict(c) for c in chunks[i]]}, ensure_ascii=False) + "\n")
        print(f"{args.n} units in {SAMPLE}")
        return 0

    sample = [json.loads(line) for line in SAMPLE.read_text().splitlines()]
    if args.command == "label":
        var = "ANTHROPIC_API_KEY" if args.model.startswith("claude") else "ZAI_API_KEY"
        key = os.environ.get(var, "")
        got, usage = {}, collections.Counter()
        for s in sample:
            payload = [{"id": c["id"], "moves": c["recent"], "said": c["text"]} for c in s["batch"]]
            labels, use = classify.ask(args.model, payload, key)
            usage.update(use)
            got[s["id"]] = next((lab for lab in labels if lab.get("id") == s["id"]), None)
        model_file(args.model).write_text(json.dumps({"labels": got, "usage": dict(usage)}, ensure_ascii=False, indent=1))
        print(f"{len(got)} labelled, {usage['prompt_tokens']} in / {usage['completion_tokens']} out tokens")
        return 0

    hand = json.loads(HAND.read_text())
    got = json.loads(model_file(args.model).read_text())["labels"]
    pairs = [(hand[s["id"]], (got.get(s["id"]) or {}).get("kind")) for s in sample if s["id"] in hand]
    agree = sum(h == m for h, m in pairs)
    print(f"{args.model}: agrees on {agree}/{len(pairs)} ({100 * agree / len(pairs):.0f}%)")
    print("hand -> model:", dict(collections.Counter(f"{h}->{m}" for h, m in pairs if h != m)))
    for s in sample:
        h, m = hand.get(s["id"]), (got.get(s["id"]) or {})
        if h and h != m.get("kind"):
            print(f"  {s['id']}: hand {h}, model {m.get('kind')}: {s['text'][:140]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
