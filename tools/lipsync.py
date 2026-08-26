#!/usr/bin/env python3
"""Make a line of Korean into audio plus a mouth-shape timeline.

    export TYPECAST_API_KEY=...
    python3 tools/lipsync.py voices                                  # what can speak
    python3 tools/lipsync.py say --voice 65bb3a1976b69213594357fc \
        --text "안녕하세요, 오늘도 좋은 하루 보내세요." --out work/speech/greeting

`say` writes into the output directory:

    speech.wav (or .mp3)   the audio as returned
    timestamps.json        the API's own word/character timings, unmodified
    visemes.json           the mouth-shape timeline a rig or a viewer plays

The timeline is deliberately a separate file from the audio: the same JSON drives a Three.js
morph-target rig, a Lottie mouth swap, or a video compositor, and none of them need to know that
Typecast produced it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from lipsync import typecast, visemes  # noqa: E402


def cmd_voices(args: argparse.Namespace) -> int:
    voices = typecast.list_voices(model=args.model, key=args.api_key)
    if not voices:
        print("no voices returned — check the model name or the API key", file=sys.stderr)
        return 1
    needle = (args.filter or "").lower()
    shown = 0
    for voice in voices:
        name = str(voice.get("voice_name") or voice.get("name") or "")
        voice_id = str(voice.get("voice_id") or voice.get("id") or "")
        emotions = voice.get("emotions") or voice.get("available_emotions") or []
        if needle and needle not in name.lower() and needle not in voice_id.lower():
            continue
        shown += 1
        print(f"{name:<24} {voice_id:<32} {', '.join(map(str, emotions))[:60]}")
    print(f"\n{shown}/{len(voices)} voice(s)", file=sys.stderr)
    return 0


def cmd_say(args: argparse.Namespace) -> int:
    text = args.text or (args.text_file.read_text(encoding="utf-8").strip() if args.text_file else "")
    if not text:
        print('provide --text "..." or --text-file', file=sys.stderr)
        return 2
    speech = typecast.speak(
        text,
        args.voice,
        model=args.model,
        language=args.language,
        granularity=args.granularity,
        seed=args.seed,
        key=args.api_key,
    )
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    audio_path = speech.write_audio(out)
    (out / "timestamps.json").write_text(json.dumps(speech.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    timeline = visemes.build(speech.characters, duration=speech.duration, source=f"typecast:{speech.voice_id}")
    (out / "visemes.json").write_text(json.dumps(timeline.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    counts: dict[str, int] = {}
    for segment in timeline.segments:
        counts[segment.viseme] = counts.get(segment.viseme, 0) + 1
    print(f"audio     {audio_path}  ({speech.duration:.2f}s, {len(speech.audio)/1024:.0f}KB {speech.audio_format})")
    print(f"timings   {out / 'timestamps.json'}  ({len(speech.characters)} character span(s))")
    print(f"visemes   {out / 'visemes.json'}  ({len(timeline.segments)} segment(s): " + ", ".join(f"{k}×{v}" for k, v in sorted(counts.items())) + ")")
    if not speech.characters:
        print("\nno character timings came back — retry with --granularity char", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api-key", dest="api_key", help="overrides TYPECAST_API_KEY")
    parser.add_argument("--model", default=typecast.DEFAULT_MODEL)
    sub = parser.add_subparsers(dest="command", required=True)

    voices = sub.add_parser("voices", help="list the voices this key can use")
    voices.add_argument("--filter", help="substring match on name or id")
    voices.set_defaults(func=cmd_voices)

    say = sub.add_parser("say", help="synthesise a line and build its viseme timeline")
    say.add_argument("--voice", required=True, help="voice id (bare 24-hex from the console is fine)")
    say.add_argument("--text", help="what to say")
    say.add_argument("--text-file", type=Path, dest="text_file", help="read the line from a file")
    say.add_argument("--out", type=Path, required=True, help="output directory")
    say.add_argument("--language", help="e.g. kor, eng (omit to let the model detect it)")
    say.add_argument("--granularity", default="char", choices=["char", "word", "both"], help="default char: a Korean character is a syllable")
    say.add_argument("--seed", type=int, help="reproducible synthesis")
    say.set_defaults(func=cmd_say)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except typecast.TypecastError as exc:
        print(f"typecast: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
