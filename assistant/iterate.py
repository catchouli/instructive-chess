"""A small, varied dev set for iterating on classify.py's prompt with Cat
reviewing each version (review.py --run TAG). The validation set (validate.py)
stays untouched, as the final test.

    scanner/.venv/bin/python assistant/iterate.py sample [--n 60] [--seed 20261021]
    scanner/.venv/bin/python assistant/iterate.py label TAG [--model claude-haiku-5-5]

`sample` draws units evenly across channels and game phases (opening, middlegame,
late), never from the validation set, and shuffles them into
assistant/cache/dev/units.json. `label` sends each in the batch classify.py would
and saves the labels as assistant/cache/dev/run_TAG.json.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import random
import sys
from dataclasses import asdict

import classify
import positions
import validate

DEV = positions.ROOT / "assistant" / "cache" / "dev"
UNITS = DEV / "units.json"


def phase(ply: int) -> str:
    return "opening" if ply <= 16 else "middlegame" if ply <= 50 else "late"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["sample", "label"])
    ap.add_argument("tag", nargs="?")
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--seed", type=int, default=20261021)
    ap.add_argument("--model", default="claude-haiku-5-5")
    args = ap.parse_args(argv)
    DEV.mkdir(parents=True, exist_ok=True)

    if args.command == "sample":
        chunks = validate.batches(json.loads(positions.CATALOG.read_text()))
        held_out = {json.loads(line)["id"] for line in validate.SAMPLE.read_text().splitlines()} \
            if validate.SAMPLE.exists() else set()
        cells: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
        for uid, chunk in chunks.items():
            if uid in held_out:
                continue
            u = next(c for c in chunk if c.id == uid)
            if u.text.strip():
                cells[(u.channel, phase(u.ply))].append(uid)
        rng = random.Random(args.seed)
        keys = sorted(cells)
        picked: list[str] = []
        while len(picked) < args.n:       # round-robin over channel x phase
            for k in keys:
                left = [i for i in cells[k] if i not in picked]
                if left and len(picked) < args.n:
                    picked.append(rng.choice(left))
        rng.shuffle(picked)
        out = []
        for uid in picked:
            u = next(c for c in chunks[uid] if c.id == uid)
            out.append(asdict(u) | {"batch": [asdict(c) for c in chunks[uid]]})
        UNITS.write_text(json.dumps(out, ensure_ascii=False))
        print(f"{len(out)} units from {len(keys)} channel x phase cells in {UNITS}")
        return 0

    if not args.tag:
        ap.error("label needs a TAG")
    units = json.loads(UNITS.read_text())
    var = "HAIKU_API_KEY" if args.model.startswith("claude") else "ZAI_API_KEY"
    key = os.environ.get(var, "")
    got, usage = {}, collections.Counter()
    for u in units:
        payload = [{"id": c["id"], "moves": c["recent"], "said": c["text"]} for c in u["batch"]]
        labels, use = classify.ask(args.model, payload, key)
        usage.update(use)
        lab = next((x for x in labels if x.get("id") == u["id"]), None)
        b = u["batch"]
        k = next(i for i, c in enumerate(b) if c["id"] == u["id"])
        got[u["id"]] = classify.checked(lab, u["text"], b[k - 1]["text"] if k else "",
                                        b[k + 1]["text"] if k + 1 < len(b) else "") if lab else None
    out = DEV / f"run_{args.tag}.json"
    out.write_text(json.dumps({"model": args.model, "labels": got, "usage": dict(usage)}, ensure_ascii=False, indent=1))
    kinds = collections.Counter((g or {}).get("kind") for g in got.values())
    bad = sum(1 for g in got.values() if g and g.get("evidence_ok") is False)
    print(f"{out}: {dict(kinds)}, {bad} instructive with evidence not found in the text, "
          f"{usage['prompt_tokens']} in / {usage['completion_tokens']} out tokens")
    return 0


if __name__ == "__main__":
    sys.exit(main())
