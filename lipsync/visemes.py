"""Hangul syllable timings → a viseme (mouth shape) timeline.

A Hangul block decomposes arithmetically into 초성 / 중성 / 종성, and for a stylized character the
mouth shape through a syllable is carried by the 중성: 아 is open and wide, 이 is narrow and
spread, 우 is small and rounded. The consonants mostly do not change the *shape* enough to matter
at this level of stylization — with one exception that does read on screen: the bilabials
ㅁ/ㅂ/ㅃ/ㅍ close the lips completely. A character that never closes its mouth on "밥" or "엄마"
looks wrong immediately, so those get an explicit closure at the start or the end of the syllable.

Five shapes plus a closed rest is what a stylized rig can actually hit:

    AA  open, wide          아 애 야 얘 와 왜
    EH  half open           어 에 여 예 외 워 웨
    OH  rounded, medium     오 요
    OO  rounded, small      우 유 으
    EE  narrow, spread      이 위 의
    MM  lips closed         ㅁ ㅂ ㅃ ㅍ, and silence

Everything that is not a Hangul block — latin text, digits, punctuation, spaces — is left as
silence rather than guessed at. That is the honest reading of a Korean-only mapping, and it keeps
a mixed-language line from producing confident nonsense.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

HANGUL_START, HANGUL_END = 0xAC00, 0xD7A3
JUNG_COUNT, JONG_COUNT = 21, 28

# 초성 indices whose lips are closed: ㅁ(6) ㅂ(7) ㅃ(8) ㅍ(17)
BILABIAL_ONSET = {6, 7, 8, 17}
# 종성 indices that end with the lips closed: ㄻ(10) ㅁ(16) ㅂ(17) ㅄ(18) ㅍ(26)
BILABIAL_CODA = {10, 16, 17, 18, 26}

SILENCE = "MM"

# 중성 index → (viseme, how open the mouth is, 0-1)
VOWELS: dict[int, tuple[str, float]] = {
    0: ("AA", 1.00),   # ㅏ
    1: ("AA", 0.90),   # ㅐ
    2: ("AA", 0.95),   # ㅑ
    3: ("AA", 0.90),   # ㅒ
    4: ("EH", 0.70),   # ㅓ
    5: ("EH", 0.65),   # ㅔ
    6: ("EH", 0.70),   # ㅕ
    7: ("EH", 0.65),   # ㅖ
    8: ("OH", 0.60),   # ㅗ
    9: ("AA", 0.85),   # ㅘ — a diphthong that lands on ㅏ; the ending is what the eye reads
    10: ("AA", 0.80),  # ㅙ
    11: ("EH", 0.60),  # ㅚ
    12: ("OH", 0.60),  # ㅛ
    13: ("OO", 0.35),  # ㅜ
    14: ("EH", 0.60),  # ㅝ
    15: ("EH", 0.60),  # ㅞ
    16: ("EE", 0.40),  # ㅟ
    17: ("OO", 0.35),  # ㅠ
    18: ("OO", 0.30),  # ㅡ — unrounded, but the opening is as small as 우 at this scale
    19: ("EE", 0.40),  # ㅢ
    20: ("EE", 0.45),  # ㅣ
}

# how much of a syllable a lip closure takes at its head or tail
CLOSURE_SHARE = 0.30
MIN_CLOSURE = 0.035  # seconds — shorter than this and the closure just reads as a flicker
# a gap longer than this between timed characters is a real pause, not co-articulation
SILENCE_GAP = 0.12


@dataclass
class Segment:
    start: float
    end: float
    viseme: str
    weight: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 4),
            "end": round(self.end, 4),
            "viseme": self.viseme,
            "weight": round(self.weight, 3),
        }


@dataclass
class Timeline:
    segments: list[Segment] = field(default_factory=list)
    duration: float = 0.0
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": "auto3d.visemes.v1",
            "duration": round(self.duration, 4),
            "visemes": sorted({segment.viseme for segment in self.segments}),
            "source": self.source,
            "segments": [segment.as_dict() for segment in self.segments],
            # keyframes are what a runtime actually interpolates: one entry per shape change
            "keys": [{"t": round(s.start, 4), "viseme": s.viseme, "weight": round(s.weight, 3)} for s in self.segments],
        }


def decompose(char: str) -> tuple[int, int, int] | None:
    """(초성, 중성, 종성) indices for a Hangul syllable block, else None."""
    code = ord(char)
    if not HANGUL_START <= code <= HANGUL_END:
        return None
    offset = code - HANGUL_START
    return offset // (JUNG_COUNT * JONG_COUNT), (offset % (JUNG_COUNT * JONG_COUNT)) // JONG_COUNT, offset % JONG_COUNT


def syllable_segments(char: str, start: float, end: float) -> list[Segment]:
    """Mouth shapes for one timed character.

    A syllable with a bilabial onset opens *from* a closure; one with a bilabial coda closes into
    it. Both can happen ("밤"), in which case the vowel keeps the middle.
    """
    parts = decompose(char)
    span = max(0.0, end - start)
    if parts is None or span <= 0:
        return [Segment(start, end, SILENCE, 0.0)] if span > 0 else []
    onset, nucleus, coda = parts
    viseme, weight = VOWELS.get(nucleus, ("AA", 0.8))

    # a closure needs a floor to be visible at all, and a ceiling so it cannot eat the syllable
    closure = min(max(MIN_CLOSURE, span * CLOSURE_SHARE), span * 0.5)
    head = closure if onset in BILABIAL_ONSET else 0.0
    tail = closure if coda in BILABIAL_CODA else 0.0
    if head + tail >= span:  # a very short syllable: keep the closure, drop the vowel
        head = span if head else 0.0
        tail = span - head
        segments = []
        if head:
            segments.append(Segment(start, start + head, SILENCE, 0.0))
        if tail:
            segments.append(Segment(start + head, end, SILENCE, 0.0))
        return segments

    segments: list[Segment] = []
    cursor = start
    if head:
        segments.append(Segment(cursor, cursor + head, SILENCE, 0.0))
        cursor += head
    segments.append(Segment(cursor, end - tail, viseme, weight))
    if tail:
        segments.append(Segment(end - tail, end, SILENCE, 0.0))
    return segments


def _merge(segments: Iterable[Segment]) -> list[Segment]:
    merged: list[Segment] = []
    for segment in segments:
        if segment.duration <= 0:
            continue
        if merged and merged[-1].viseme == segment.viseme and abs(merged[-1].end - segment.start) < 1e-6:
            merged[-1].end = segment.end
            merged[-1].weight = max(merged[-1].weight, segment.weight)
            continue
        merged.append(segment)
    return merged


def build(characters: list[dict[str, Any]], *, duration: float | None = None, source: str = "") -> Timeline:
    """Turn the API's `characters` array — {text, start, end} — into a viseme timeline.

    Pauses are inserted rather than implied: any gap wider than SILENCE_GAP becomes an explicit
    closed mouth, so a rig that simply holds the last key does not sit there gaping through a
    breath or a comma.
    """
    timed: list[Segment] = []
    previous_end = 0.0
    for entry in characters or []:
        try:
            text = str(entry.get("text") or "")
            start, end = float(entry["start"]), float(entry["end"])
        except (KeyError, TypeError, ValueError):
            continue
        hole = start - previous_end
        if hole > SILENCE_GAP:
            timed.append(Segment(previous_end, start, SILENCE, 0.0))
        elif hole > 0 and timed:
            # too short to be a pause: the mouth is travelling between shapes, so hold the last
            # one into it rather than leaving a hole a runtime would have to guess how to fill
            timed[-1].end = start
        for char in text:
            timed.extend(syllable_segments(char, start, end))
            # a multi-character entry shares one span; splitting it evenly would invent precision
            break
        previous_end = max(previous_end, end)

    total = float(duration) if duration else previous_end
    if total > previous_end + 1e-6:
        timed.append(Segment(previous_end, total, SILENCE, 0.0))
    segments = _merge(timed)
    if segments and segments[0].start > 1e-6:
        segments.insert(0, Segment(0.0, segments[0].start, SILENCE, 0.0))
    return Timeline(segments=segments, duration=total, source=source)
