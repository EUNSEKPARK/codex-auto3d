"""Viseme mapping and the Typecast client, exercised without an API key."""

from __future__ import annotations

import base64
import json
import unittest
from unittest import mock

from support import REPO_ROOT  # noqa: E402,F401  (puts the project root on sys.path)

from lipsync import typecast, visemes  # noqa: E402


def spans(text: str, *, per_char: float = 0.2, gap: float = 0.0) -> list[dict]:
    """Evenly timed character spans, the shape the API returns."""
    out, cursor = [], 0.0
    for char in text:
        out.append({"text": char, "start": round(cursor, 4), "end": round(cursor + per_char, 4)})
        cursor += per_char + gap
    return out


class DecomposeTest(unittest.TestCase):
    def test_syllables_split_into_jamo_indices(self) -> None:
        self.assertEqual(visemes.decompose("가"), (0, 0, 0))
        self.assertEqual(visemes.decompose("맘"), (6, 0, 16))  # ㅁ ㅏ ㅁ
        self.assertEqual(visemes.decompose("힣"), (18, 20, 27))

    def test_non_hangul_is_not_decomposed(self) -> None:
        for char in ("A", "7", " ", "!", "あ"):
            self.assertIsNone(visemes.decompose(char), char)


class VowelShapeTest(unittest.TestCase):
    def visemes_of(self, text: str) -> list[str]:
        timeline = visemes.build(spans(text))
        return [segment.viseme for segment in timeline.segments]

    def test_each_vowel_group_gets_its_own_shape(self) -> None:
        self.assertEqual(self.visemes_of("아"), ["AA"])
        self.assertEqual(self.visemes_of("어"), ["EH"])
        self.assertEqual(self.visemes_of("오"), ["OH"])
        self.assertEqual(self.visemes_of("우"), ["OO"])
        self.assertEqual(self.visemes_of("이"), ["EE"])

    def test_open_vowels_carry_more_weight_than_narrow_ones(self) -> None:
        weight = {v: visemes.build(spans(v)).segments[0].weight for v in "아이우"}
        self.assertGreater(weight["아"], weight["이"])
        self.assertGreater(weight["아"], weight["우"])

    def test_w_diphthongs_land_on_the_vowel_they_end_with(self) -> None:
        self.assertEqual(self.visemes_of("와"), ["AA"])
        self.assertEqual(self.visemes_of("워"), ["EH"])
        self.assertEqual(self.visemes_of("위"), ["EE"])


class BilabialTest(unittest.TestCase):
    def test_a_bilabial_onset_opens_from_a_closed_mouth(self) -> None:
        timeline = visemes.build(spans("바"))
        self.assertEqual([s.viseme for s in timeline.segments], ["MM", "AA"])
        self.assertAlmostEqual(timeline.segments[0].start, 0.0)

    def test_a_bilabial_coda_closes_the_mouth_at_the_end(self) -> None:
        timeline = visemes.build(spans("압"))
        self.assertEqual([s.viseme for s in timeline.segments], ["AA", "MM"])
        self.assertAlmostEqual(timeline.segments[-1].end, 0.2)

    def test_both_ends_close_around_the_vowel(self) -> None:
        self.assertEqual([s.viseme for s in visemes.build(spans("밤")).segments], ["MM", "AA", "MM"])

    def test_엄마_never_leaves_the_mouth_hanging_open(self) -> None:
        shapes = [s.viseme for s in visemes.build(spans("엄마")).segments]
        self.assertEqual(shapes, ["EH", "MM", "AA"], "the ㅁ coda and the ㅁ onset merge into one closure")

    def test_a_syllable_too_short_for_a_closure_and_a_vowel_keeps_the_closure(self) -> None:
        timeline = visemes.build(spans("밤", per_char=0.04))
        self.assertEqual({s.viseme for s in timeline.segments}, {"MM"})


class TimelineTest(unittest.TestCase):
    def test_gaps_become_explicit_silence(self) -> None:
        timeline = visemes.build(spans("아아", per_char=0.2, gap=0.5))
        self.assertEqual([s.viseme for s in timeline.segments], ["AA", "MM", "AA"])

    def test_short_gaps_are_coarticulation_not_a_pause(self) -> None:
        timeline = visemes.build(spans("아아", per_char=0.2, gap=0.05))
        self.assertEqual([s.viseme for s in timeline.segments], ["AA"], "the same shape either side merges")

    def test_trailing_audio_after_the_last_syllable_closes_the_mouth(self) -> None:
        timeline = visemes.build(spans("아"), duration=1.5)
        self.assertEqual(timeline.segments[-1].viseme, "MM")
        self.assertAlmostEqual(timeline.duration, 1.5)

    def test_non_korean_text_is_left_silent_rather_than_guessed(self) -> None:
        timeline = visemes.build(spans("Hi!"))
        self.assertEqual({s.viseme for s in timeline.segments}, {"MM"})

    def test_segments_tile_the_timeline_without_gaps_or_overlaps(self) -> None:
        timeline = visemes.build(spans("안녕하세요"), duration=1.2)
        for earlier, later in zip(timeline.segments, timeline.segments[1:]):
            self.assertAlmostEqual(earlier.end, later.start, places=6)
        self.assertAlmostEqual(timeline.segments[0].start, 0.0)
        self.assertAlmostEqual(timeline.segments[-1].end, 1.2)

    def test_serialised_timeline_carries_keys_a_runtime_can_interpolate(self) -> None:
        payload = visemes.build(spans("안녕하세요"), duration=1.2, source="test").as_dict()
        self.assertEqual(payload["format"], "auto3d.visemes.v1")
        self.assertEqual(len(payload["keys"]), len(payload["segments"]))
        self.assertEqual(payload["keys"][0]["t"], payload["segments"][0]["start"])
        json.dumps(payload)  # must round-trip

    def test_malformed_entries_are_skipped_not_fatal(self) -> None:
        timeline = visemes.build([{"text": "아", "start": 0.0, "end": 0.2}, {"text": "이"}, {"start": "x", "end": 1}])
        self.assertEqual([s.viseme for s in timeline.segments], ["AA"])


class ClientTest(unittest.TestCase):
    def test_console_ids_are_normalised_for_the_api(self) -> None:
        self.assertEqual(typecast.normalize_voice_id("65bb3a1976b69213594357fc"), "tc_65bb3a1976b69213594357fc")
        self.assertEqual(typecast.normalize_voice_id("tc_65bb3a1976b69213594357fc"), "tc_65bb3a1976b69213594357fc")
        self.assertEqual(typecast.normalize_voice_id("some-name"), "some-name")

    def test_a_missing_key_says_where_to_get_one(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(typecast.TypecastError) as caught:
                typecast.api_key()
        self.assertIn("TYPECAST_API_KEY", str(caught.exception))

    def test_speak_parses_audio_and_timings(self) -> None:
        payload = {
            "audio": base64.b64encode(b"RIFFfake").decode(),
            "audio_format": "wav",
            "audio_duration": 0.8,
            "characters": spans("안녕"),
            "words": [{"text": "안녕", "start": 0.0, "end": 0.4}],
        }
        with mock.patch.object(typecast, "_request", return_value=payload) as request:
            speech = typecast.speak("안녕", "65bb3a1976b69213594357fc", key="k")
        self.assertEqual(speech.audio, b"RIFFfake")
        self.assertEqual(speech.duration, 0.8)
        self.assertEqual(speech.voice_id, "tc_65bb3a1976b69213594357fc")
        path, kwargs = request.call_args[0][0], request.call_args[1]
        self.assertIn("/v1/text-to-speech/with-timestamps", path)
        self.assertIn("granularity=char", path, "Korean needs character granularity, not word")
        self.assertEqual(kwargs["payload"]["voice_id"], "tc_65bb3a1976b69213594357fc")

    def test_empty_text_is_refused_before_a_request_is_made(self) -> None:
        with mock.patch.object(typecast, "_request") as request:
            with self.assertRaises(typecast.TypecastError):
                typecast.speak("   ", "voice", key="k")
        request.assert_not_called()

    def test_a_response_without_audio_is_an_error_not_a_crash(self) -> None:
        with mock.patch.object(typecast, "_request", return_value={"detail": "quota exceeded"}):
            with self.assertRaises(typecast.TypecastError) as caught:
                typecast.speak("안녕", "voice", key="k")
        self.assertIn("unexpected response", str(caught.exception))

    def test_end_to_end_shape_from_a_recorded_response(self) -> None:
        payload = {
            "audio": base64.b64encode(b"RIFF").decode(),
            "audio_format": "wav",
            "audio_duration": 1.0,
            "characters": spans("반갑습니다"),
        }
        with mock.patch.object(typecast, "_request", return_value=payload):
            speech = typecast.speak("반갑습니다", "voice", key="k")
        timeline = visemes.build(speech.characters, duration=speech.duration)
        self.assertEqual(timeline.segments[0].viseme, "MM", "반 starts on ㅂ")
        self.assertAlmostEqual(timeline.segments[-1].end, 1.0)


if __name__ == "__main__":
    unittest.main()
