"""Command line entry point.

    avr doctor    check every prerequisite before you rely on it
    avr peek      print the current card as the grader sees it
    avr grade     grade a transcript against an answer, no mic needed
    avr listen    print live transcription, prove the echo gate works
    avr review    the hands-free loop
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time

from .anki import AnkiConnect, AnkiError
from .config import Config
from .grade import grade
from .runner import Runner
from .stt import Transcriber, TranscriberError
from .tts import Speaker, SpeakerError

OK, BAD = "  ok  ", " FAIL "


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    """Check everything the review loop depends on, and say exactly how to fix each gap."""
    from .diagnostics import blocking_problems, run_all

    if sys.platform != "darwin":
        print(f"[{BAD}] platform — {sys.platform}, but this runs on macOS only")
        print(
            "\nThis must run on the Mac. Anki, the microphone, whisper.cpp, `say`, and Ollama\n"
            "all live there; the OptiPlex is the development box only."
        )
        return 1

    problems = cfg.validate()
    print(f"[{OK if not problems else BAD}] config" + (f"  — {problems[0]}" if problems else ""))

    checks = run_all(cfg, include_anki=AnkiConnect(cfg.anki_url))
    for check in checks:
        mark = OK if check.good else BAD
        print(f"[{mark}] {check.name}" + (f"  — {check.detail}" if check.detail else ""))

    broken = [c for c in checks if not c.good]
    if broken or problems:
        print("\nTo fix:\n")
        for problem in problems:
            print(f"  - config: {problem}\n")
        for check in broken:
            print(f"  - {check.name}: {check.detail}")
            if check.fix:
                for line in check.fix.splitlines():
                    print(f"      {line}")
            print()
        return 1 if (blocking_problems(checks) or problems) else 0

    print("\nAll good. Run `avr review` to start.")
    return 0


def cmd_peek(cfg: Config, args: argparse.Namespace) -> int:
    anki = AnkiConnect(cfg.anki_url)
    try:
        card = anki.preflight()
    except AnkiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"card id : {card.card_id}")
    print(f"spoken  : {card.question}")
    print(f"graded  : {card.answer}")
    if args.raw:
        print(f"\n--- raw question html ---\n{card.raw_question_html}")
        print(f"\n--- raw answer html ---\n{card.raw_answer_html}")
    return 0


def cmd_grade(cfg: Config, args: argparse.Namespace) -> int:
    verdict = grade(args.question, args.answer, args.transcript, cfg)
    print(f"{'CORRECT' if verdict.correct else 'INCORRECT'}")
    print(f"score   : {verdict.score:.3f}")
    print(f"decided : {verdict.source} ({verdict.detail})")
    return 0


def cmd_listen(cfg: Config, args: argparse.Namespace) -> int:
    """Prove the mic works and that the TTS does not get transcribed back."""
    transcriber = Transcriber(
            cfg.whisper_bin,
            cfg.whisper_model,
            threads=cfg.whisper_threads,
            length_ms=cfg.whisper_length_ms,
            vad_threshold=cfg.vad_threshold,
            step_ms=cfg.whisper_step_ms,
        )
    speaker = Speaker(cfg.say_voice, cfg.say_rate)
    try:
        transcriber.preflight()
        speaker.preflight()
    except (TranscriberError, SpeakerError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("Starting whisper-stream. Ctrl-C to stop.")
    print("(macOS may ask for microphone permission the first time.)\n")
    transcriber.start()
    try:
        time.sleep(2)  # let the model load before the echo test
        phrase = "Echo gate test. This sentence is spoken by the computer and must not appear below."
        print(f"speaking: {phrase!r}\n")
        speaker.speak(phrase, gate=transcriber)
        print("Gate reopened. Anything below this line came from the microphone:\n")

        while True:
            line = transcriber.get(timeout=0.5)
            if line:
                print(f"  heard: {line}")
    except KeyboardInterrupt:
        print("\nstopped")
        return 0
    finally:
        transcriber.stop()


def cmd_review(cfg: Config, args: argparse.Namespace) -> int:
    problems = cfg.validate()
    if problems:
        for problem in problems:
            print(f"config error: {problem}", file=sys.stderr)
        return 2

    transcriber = Transcriber(
            cfg.whisper_bin,
            cfg.whisper_model,
            threads=cfg.whisper_threads,
            length_ms=cfg.whisper_length_ms,
            vad_threshold=cfg.vad_threshold,
            step_ms=cfg.whisper_step_ms,
        )
    speaker = Speaker(cfg.say_voice, cfg.say_rate)
    anki = AnkiConnect(cfg.anki_url)

    try:
        transcriber.preflight()
        speaker.preflight()
    except (TranscriberError, SpeakerError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    runner = Runner(cfg, anki, transcriber, speaker)
    print(f"Say your answer, then '{cfg.terminator}'.")
    print("Commands: again · hard · good · easy · repeat · skip · quit\n")

    try:
        runner.run()
    except AnkiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(runner.summary())
    return 0


def cmd_selftest(cfg: Config, args: argparse.Namespace) -> int:
    """Drive the whole loop against real Anki, scripting the microphone.

    Creates its own deck, reviews only cards it created, and deletes them afterwards.
    """
    from .selftest import run as run_selftest

    try:
        return run_selftest(cfg, keep=args.keep)
    except AnkiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="avr", description="Hands-free voice review for Anki (macOS only)."
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check every prerequisite").set_defaults(func=cmd_doctor)

    peek = sub.add_parser("peek", help="print the current card as the grader sees it")
    peek.add_argument("--raw", action="store_true", help="also dump the raw HTML")
    peek.set_defaults(func=cmd_peek)

    grade_cmd = sub.add_parser("grade", help="grade a transcript without a mic")
    grade_cmd.add_argument("answer", help="the card's answer text")
    grade_cmd.add_argument("transcript", help="what was said")
    grade_cmd.add_argument("--question", default="", help="question text, helps the judge")
    grade_cmd.set_defaults(func=cmd_grade)

    sub.add_parser("listen", help="live transcription + echo gate test").set_defaults(
        func=cmd_listen
    )

    selftest = sub.add_parser(
        "selftest",
        help="end-to-end test against live Anki using a throwaway deck (no mic needed)",
    )
    selftest.add_argument(
        "--keep", action="store_true", help="leave the throwaway deck behind for inspection"
    )
    selftest.set_defaults(func=cmd_selftest)

    sub.add_parser("review", help="run the hands-free review loop").set_defaults(func=cmd_review)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(Config(), args)


if __name__ == "__main__":
    raise SystemExit(main())
