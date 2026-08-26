"""Speech → mouth shapes, for making a generated model talk.

Typecast's `/v1/text-to-speech/with-timestamps` returns the audio *and* per-word / per-character
timings in one response, which is the hard half of lip-sync solved for us: no forced alignment, no
phoneme recognizer. Korean is written in syllable blocks, so a character timestamp is a syllable
timestamp, and a syllable's mouth shape is decided almost entirely by its vowel — decompose the
block and the viseme falls out.

    lipsync.typecast   the API client (standard library only, like the rest of this repo)
    lipsync.visemes    Hangul → viseme timeline

Nothing here touches the 3D pipeline; the timeline is a plain JSON file that a viewer, a Lottie
renderer or a morph-target rig can each read.
"""

from __future__ import annotations

__version__ = "0.1.0"
