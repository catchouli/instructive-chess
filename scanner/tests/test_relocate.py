import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import relocate  # noqa: E402


def _readings(pattern):
    """one reading a second from a string: '#' a board read, '.' none"""
    return [(float(i), "x" if c == "#" else None) for i, c in enumerate(pattern)]


def test_a_minute_mostly_unread_is_a_poor_stretch():
    good, bad = "#" * 120, ("#..." * 30)          # bad: a quarter read, 120 s
    stretches = relocate.poor_stretches(_readings(good + bad + good))
    assert len(stretches) == 1
    a, b = stretches[0]
    assert 120 - 60 < a <= 121 and 239 <= b <= 240 + 60


def test_a_short_gap_or_half_read_stretch_is_not():
    assert relocate.poor_stretches(_readings("#" * 120 + "." * 20 + "#" * 120)) == []
    assert relocate.poor_stretches(_readings("#" * 120 + "#." * 60 + "#" * 120)) == []


def test_nothing_before_the_first_readable_minute():
    assert relocate.poor_stretches(_readings("." * 100 + "#" * 30 + "#" * 120)) == []


class _Read:
    def __init__(self, n_pieces, confidence):
        self.n_pieces, self.confidence = n_pieces, confidence


def test_reads_better_needs_a_confident_read_with_clearly_more_pieces():
    assert relocate.reads_better(_Read(24, 0.84), _Read(6, 0.84))       # a theme the old templates miss
    assert relocate.reads_better(_Read(24, 0.84), _Read(24, 0.60))      # the old read isn't confident
    assert not relocate.reads_better(_Read(24, 0.84), _Read(22, 0.84))  # about the same
    assert not relocate.reads_better(_Read(24, 0.60), _Read(6, 0.84))   # the new read isn't confident
