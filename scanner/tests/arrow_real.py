#!/usr/bin/env python3
"""Score arrows.py on real frames labelled by hand (tests/fixtures/arrows_real.json).

    scanner/.venv/bin/python scanner/tests/arrow_real.py

The board crops are video frames, so they stay in scanner/cache/arrows_real/
(local); the fixture keeps the video, time and the arrows seen.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import arrows  # noqa: E402


def main() -> int:
    frames = json.loads((ROOT / "tests" / "fixtures" / "arrows_real.json").read_text())["frames"]
    for site in ("chesscom", "lichess"):
        tp = fp = fn = 0
        for f in [x for x in frames if x["site"] == site]:
            img = cv2.imread(str(ROOT / "cache" / "arrows_real" / f["file"]))
            if img is None:
                print(f"  missing {f['file']}")
                continue
            got = {str(a) for a in arrows.detect(img, f["flipped"], site)}
            truth = set(f["arrows"])
            tp, fp, fn = tp + len(got & truth), fp + len(got - truth), fn + len(truth - got)
            if got != truth:
                print(f"  {f['file']}: truth {sorted(truth)}, found {sorted(got)}")
        print(f"{site}: precision {tp}/{tp + fp}, recall {tp}/{tp + fn}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
