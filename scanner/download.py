"""Fetch a YouTube video (or accept a local file) plus its metadata.

Downloads land in scanner/cache/ and are reused on subsequent runs.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

CACHE = Path(__file__).resolve().parent / "cache"

_YT_ID = re.compile(r"(?:v=|/shorts/|youtu\.be/|/embed/)([0-9A-Za-z_-]{11})")


@dataclass
class Source:
    video_id: str
    path: Path            # local media file to scan
    title: str
    channel: str
    published: str        # ISO date (YYYY-MM-DD) or "" if unknown
    url: str              # canonical watch URL, or file:// for local
    duration: float       # seconds, 0 if unknown


def _looks_like_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def video_id_from_url(url: str) -> str:
    m = _YT_ID.search(url)
    if m:
        return m.group(1)
    # fall back to a slug of the whole URL
    return re.sub(r"[^0-9A-Za-z_-]", "_", url)[-32:]


def _run(cmd: list[str], timeout: float | None = None) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"command timed out after {timeout}s: {' '.join(cmd)}")
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr}")
    return proc.stdout


def _ytdlp() -> str:
    # prefer the venv's yt-dlp
    local = Path(__file__).resolve().parent / ".venv" / "bin" / "yt-dlp"
    return str(local) if local.exists() else "yt-dlp"


def fetch(target: str, max_height: int = 720) -> Source:
    """Resolve *target* (URL or path) to a local media file + metadata."""
    CACHE.mkdir(parents=True, exist_ok=True)

    if not _looks_like_url(target):
        p = Path(target).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(p)
        # a file this tool downloaded itself (e.g. re-scanning scanner/cache/<id>.ext
        # directly, to reuse the cache without re-downloading) carries its real
        # metadata in the matching <id>.info.json - use it instead of silently
        # degrading to a bare filename with no title/channel/date/URL.
        meta_path = p.parent / f"{p.stem}.info.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            upload = str(meta.get("upload_date") or "")
            published = f"{upload[:4]}-{upload[4:6]}-{upload[6:8]}" if len(upload) == 8 else ""
            return Source(
                video_id=p.stem,
                path=p,
                title=meta.get("title") or p.stem,
                channel=meta.get("channel") or meta.get("uploader") or "",
                published=published,
                url=meta.get("webpage_url") or p.as_uri(),
                duration=float(meta.get("duration") or 0.0),
            )
        return Source(
            video_id=re.sub(r"[^0-9A-Za-z_-]", "_", p.stem)[-32:],
            path=p,
            title=p.stem,
            channel="",
            published="",
            url=p.as_uri(),
            duration=0.0,
        )

    vid = video_id_from_url(target)
    meta_path = CACHE / f"{vid}.info.json"
    if not meta_path.exists():
        out = _run([_ytdlp(), "--dump-single-json", "--no-warnings", target])
        meta_path.write_text(out)
    meta = json.loads(meta_path.read_text())

    upload = str(meta.get("upload_date") or "")
    published = f"{upload[:4]}-{upload[4:6]}-{upload[6:8]}" if len(upload) == 8 else ""

    media = next((m for m in CACHE.glob(f"{vid}.*") if m.suffix not in {".json", ".png"}), None)
    if media is None:
        fmt = f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b"
        _run([
            _ytdlp(), "-f", fmt, "--no-warnings",
            "-o", str(CACHE / f"{vid}.%(ext)s"),
            target,
        ])
        media = next(m for m in CACHE.glob(f"{vid}.*") if m.suffix not in {".json"})

    return Source(
        video_id=vid,
        path=media,
        title=meta.get("title") or vid,
        channel=meta.get("channel") or meta.get("uploader") or "",
        published=published,
        url=meta.get("webpage_url") or target,
        duration=float(meta.get("duration") or 0.0),
    )


def fetch_hires_frame(url: str, timestamp: float, max_height: int = 1080) -> np.ndarray | None:
    """Grab one high-resolution frame near `timestamp`, without downloading
    (or permanently caching) the whole video at that resolution.

    The regular scan resolution (720p by default) is fine for board/clock
    reading but too coarse for rating digits - confirmed while hand-building
    rating_read.py's templates, where a rating's digits were only a few
    pixels tall and touching at 720p. Since a rating never changes during a
    game, only a single good frame is ever needed, so it's cheaper to fetch
    a few seconds of a higher-resolution stream than to redo the whole scan
    at that resolution. Returns None for anything this can't apply to (a
    local file has no "higher-resolution" version to fetch) or that fails
    (network hiccup, an unavailable format) - a missing rating reading is
    expected and handled by callers already, same as clock_read.py's own
    best-effort philosophy."""
    if not _looks_like_url(url):
        return None
    # Kept losslessly once fetched, so a rescan reads ratings without the
    # network (~10-45 s a fetch; up to three per game)
    key = hashlib.sha1(f"{url}|{timestamp:.2f}|{max_height}".encode()).hexdigest()[:16]
    cached = CACHE / "hires" / f"{key}.png"
    if cached.exists():
        return cv2.imread(str(cached))
    frame = _fetch_hires_frame(url, timestamp, max_height)
    if frame is not None:
        cached.parent.mkdir(exist_ok=True)
        tmp = cached.with_suffix(".tmp.png")
        cv2.imwrite(str(tmp), frame, [cv2.IMWRITE_PNG_COMPRESSION, 3])
        tmp.replace(cached)
    return frame


def _fetch_hires_frame(url: str, timestamp: float, max_height: int) -> np.ndarray | None:
    start = max(0.0, timestamp - 2.0)
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        try:
            # A per-game fetch like this can run many times in one scan (one
            # per game found), unlike the single whole-video fetch() above -
            # a bounded timeout keeps one slow/hung network call from
            # stalling an entire scan rather than just costing one rating.
            _run([
                _ytdlp(), "-f", f"bestvideo[height<={max_height}]", "--no-warnings",
                "--download-sections", f"*{start}-{start + 6}",
                "-o", str(tmp / "clip.%(ext)s"),
                url,
            ], timeout=45)
        except RuntimeError:
            return None
        clip = next((p for p in tmp.glob("clip.*")), None)
        if clip is None:
            return None
        cap = cv2.VideoCapture(str(clip))
        cap.set(cv2.CAP_PROP_POS_MSEC, (timestamp - start) * 1000)
        ok, frame = cap.read()
        cap.release()
        return frame if ok else None
