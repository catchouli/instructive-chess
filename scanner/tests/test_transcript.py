"""transcript.py: YouTube json3 captions -> timed segments."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import transcript  # noqa: E402

JSON3 = {"events": [
    {"tStartMs": 0, "dDurationMs": 100, "wWinId": 1},                       # window marker, no words
    {"tStartMs": 62450, "dDurationMs": 2620, "aAppend": 1, "segs": [{"utf8": "\n"}]},
    {"tStartMs": 62460, "dDurationMs": 4470, "segs": [
        {"utf8": "run"}, {"utf8": " by", "tOffsetMs": 180}, {"utf8": " the", "tOffsetMs": 210}]},
    {"tStartMs": 65000, "dDurationMs": 3000, "segs": [{"utf8": "st."}, {"utf8": " Louis\n", "tOffsetMs": 300}]},
]}


def test_parse_json3_keeps_only_worded_events_in_seconds():
    segs = transcript.parse_json3(JSON3)
    assert [s["text"] for s in segs] == ["run by the", "st. Louis"]
    assert segs[0]["start"] == 62.46
    assert segs[1] == {"start": 65.0, "end": 68.0, "text": "st. Louis"}


def test_parse_json3_trims_overlap_to_next_start():
    segs = transcript.parse_json3(JSON3)
    # 62.46 + 4.47 = 66.93 would overlap the next segment, which starts at 65.0
    assert segs[0]["end"] == 65.0


def test_parse_json3_empty():
    assert transcript.parse_json3({}) == []
