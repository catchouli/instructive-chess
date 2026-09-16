"""Fetch a YouTube video (or accept a local file) plus its metadata.

Downloads land in scanner/cache/ and are reused on subsequent runs.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

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


def _run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
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
