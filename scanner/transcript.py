#!/usr/bin/env python3
"""Fetch a video's YouTube transcript (the uploader's captions if any, else
YouTube's automatic ones) and keep it as timed segments, so the narrator's
commentary can later be lined up with the moves the scanner timed.

    scanner/.venv/bin/python scanner/transcript.py VIDEO_ID_OR_URL [...]
    scanner/.venv/bin/python scanner/transcript.py --all      # every video in the catalog
    ... --force    refetch ones already saved

Written to transcripts/<video_id>.json (in git - a two-hour stream is
~100-150 KB):

    {"video_id": ..., "lang": "en", "kind": "auto" | "manual",
     "fetched": "YYYY-MM-DD", "duration": <video seconds>,
     "segments": [{"start": 62.46, "end": 66.93, "text": "run by the ..."}, ...]}

The transcript is deliberately kept as its own timed stream rather than
split by move: commentary lags a move by seconds (or precedes it: "I'm
going to play Nf3 here"), and a video's move times change whenever it's
rescanned, so any join to the moves belongs at query time, using the
catalog's per-ply move_seconds. Word-level timings exist in the source
(json3) but aren't kept - the fetch takes seconds, so refetch if they're
ever needed. Automatic captions mangle chess notation ("night f3", "before"
for g4); that is left as is - the catalog's moves are the ground truth.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import download

ROOT = Path(__file__).resolve().parents[1]
TRANSCRIPTS = ROOT / "transcripts"
CATALOG = ROOT / "web" / "data" / "games.json"
LANG = "en"


def parse_json3(data: dict) -> list[dict]:
    """YouTube's json3 caption events -> [{start, end, text}] in seconds.
    Events with no words (window/newline markers) are dropped; a
    segment's end is the start of the next one when that's sooner than
    its own duration, since auto captions overlap their neighbours."""
    segs = []
    for ev in data.get("events", []):
        text = "".join(s.get("utf8", "") for s in ev.get("segs", [])).replace("\n", " ").strip()
        if not text:
            continue
        start = ev.get("tStartMs", 0) / 1000.0
        end = start + ev.get("dDurationMs", 0) / 1000.0
        segs.append({"start": round(start, 2), "end": round(end, 2), "text": text})
    for a, b in zip(segs, segs[1:]):
        if b["start"] < a["end"]:
            a["end"] = b["start"]
    return segs


def fetch(target: str, lang: str = LANG) -> dict | None:
    """The transcript of a YouTube video, or None if it has none in *lang*.
    The uploader's own captions win over automatic ones."""
    vid = download.video_id_from_url(target) if download._looks_like_url(target) else target
    url = f"https://www.youtube.com/watch?v={vid}"
    with tempfile.TemporaryDirectory(prefix="instructive-chess-tr-") as tmp:
        for kind, flag in (("manual", "--write-subs"), ("auto", "--write-auto-subs")):
            download._run([download._ytdlp(), "--skip-download", flag, "--sub-langs", lang,
                           "--sub-format", "json3", "--no-warnings", "-o", f"{tmp}/%(id)s", url])
            got = Path(tmp) / f"{vid}.{lang}.json3"
            if got.exists():
                segments = parse_json3(json.loads(got.read_text()))
                break
        else:
            return None
    meta_path = download.CACHE / f"{vid}.info.json"
    duration = json.loads(meta_path.read_text()).get("duration", 0) if meta_path.exists() else 0
    return {"video_id": vid, "lang": lang, "kind": kind, "fetched": date.today().isoformat(),
            "duration": duration, "segments": segments}


def save(target: str, out_dir: Path = TRANSCRIPTS, force: bool = False) -> Path | None:
    """Fetch and write transcripts/<video_id>.json; None when the video has
    no transcript. An existing file is kept unless *force*."""
    vid = download.video_id_from_url(target) if download._looks_like_url(target) else target
    out = out_dir / f"{vid}.json"
    if out.exists() and not force:
        return out
    tr = fetch(vid)
    if tr is None:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(tr, ensure_ascii=False, separators=(",", ":")) + "\n")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="*", help="video IDs or URLs")
    ap.add_argument("--all", action="store_true", help="every video in the catalog")
    ap.add_argument("--force", action="store_true", help="refetch transcripts already saved")
    ap.add_argument("--jobs", type=int, default=1, help="parallel fetches (YouTube answers 429 to more than one or two)")
    args = ap.parse_args(argv)
    targets = list(args.targets)
    if args.all:
        seen = set(targets)
        for g in json.loads(CATALOG.read_text()):
            if g["video_id"] not in seen:
                seen.add(g["video_id"])
                targets.append(g["video_id"])
    if not targets:
        ap.error("give video IDs/URLs or --all")

    def one(t: str) -> tuple[str, str]:
        try:
            out = save(t, force=args.force)
        except Exception as e:  # noqa: BLE001 - one video shouldn't stop the batch
            return t, f"failed: {e!s:.120}"
        if out is None:
            return t, "no transcript"
        n = len(json.loads(out.read_text())["segments"])
        return t, f"{n} segments -> {out.relative_to(ROOT)}"

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for i, fut in enumerate(as_completed([pool.submit(one, t) for t in targets]), 1):
            t, status = fut.result()
            print(f"[{i}/{len(targets)}] {t}: {status}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
