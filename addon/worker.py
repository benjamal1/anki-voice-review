"""The voice loop, running off the GUI thread.

Same shape as the CLI runner: the state machine decides, this executes. The difference is that
every Anki action is marshalled to the main thread by the bridge, and progress is reported
through callbacks so the dialog can show what is happening.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from .avr.config import Config
from .avr.grade import grade, prewarm
from .avr.session import (
    match_command,
    AnswerCard,
    BuryCard,
    FlagCard,
    RegradeCard,
    UndoCard,
    NextCard,
    Quit,
    Session,
    ShowAnswer,
    Speak,
    StartOverrideTimer,
)
from .avr.stt import Transcriber, TranscriberError
from .avr.tts import Speaker, SpeakerError
from .bridge import AnkiBridge, BridgeError, NoCardShowing
from . import tracelog

log = logging.getLogger(__name__)

POLL_S = 0.15

# Phases reported to the UI. Strings rather than an enum so the dialog stays dumb.
PHASE_LOADING = "loading"
PHASE_SPEAKING = "speaking"
PHASE_LISTENING = "listening"
PHASE_GRADING = "grading"
PHASE_VERDICT = "verdict"
PHASE_AWAITING = "awaiting"
PHASE_STOPPED = "stopped"


class VoiceWorker(threading.Thread):
    def __init__(
        self,
        cfg: Config,
        bridge: AnkiBridge,
        on_phase: Callable[[str, str], None],
        on_card: Callable[[str], None],
        on_heard: Callable[[str], None],
        on_verdict: Callable[[bool, float, str, str], None],
        on_error: Callable[[str], None],
        on_finished: Callable[[str], None],
    ) -> None:
        super().__init__(daemon=True, name="avr-voice-worker")
        self.cfg = cfg
        self.bridge = bridge
        self.on_phase = on_phase
        self.on_card = on_card
        self.on_heard = on_heard
        self.on_verdict = on_verdict
        self.on_error = on_error
        self.on_finished = on_finished

        self.session = Session(cfg=cfg, grade_fn=grade)
        self.stt = Transcriber(
            cfg.whisper_bin,
            cfg.whisper_model,
            threads=cfg.whisper_threads,
            length_ms=cfg.whisper_length_ms,
            vad_threshold=cfg.vad_threshold,
            step_ms=cfg.whisper_step_ms,
        )
        self.tts = Speaker(cfg.say_voice, cfg.say_rate)

        # NOT self._stop: threading.Thread already defines a private _stop() method, and
        # overwriting it makes is_alive() raise "'Event' object is not callable" the moment
        # anything checks whether the thread is running.
        self._stopping = threading.Event()
        self._override_deadline: Optional[float] = None
        self._last_card_id = 0

    def request_stop(self) -> None:
        """Stop now, not at the end of the current sentence.

        Interrupting the speaker matters twice over: it is what the user asked for when they
        pressed Stop, and the worker is otherwise blocked inside `say` and cannot reach the
        code that releases the microphone — so a restart would find whisper still holding it
        and the new process would exit immediately.
        """
        self._stopping.set()
        self.tts.interrupt()

    def shutdown(self, timeout: float = 5.0) -> bool:
        """Stop and wait for the thread to actually finish. True if it did."""
        self.request_stop()
        self.join(timeout=timeout)
        if self.is_alive():
            return False
        self.stt.stop()  # belt and braces; run() already does this in its finally
        return True

    # --- intent execution ---

    def _execute(self, intents: list) -> None:
        pending = list(intents)
        while pending and not self._stopping.is_set():
            intent = pending.pop(0)
            try:
                if self._run_intent(intent, pending) is False:
                    return
            except Exception as exc:  # noqa: BLE001
                # One failing action must not take the session with it. Before this, a single
                # raise inside bury/flag/undo propagated out of the run loop and ended the
                # review with no visible cause — indistinguishable from "it did nothing".
                log.exception("intent %s failed", type(intent).__name__)
                tracelog.write("INTENT-FAILED", f"{type(intent).__name__}: {exc}")
                self.on_error(f"{type(intent).__name__} failed: {exc}")

    def _run_intent(self, intent, pending: list):
        """Perform one intent. Return False to stop executing the rest of this batch."""
        tracelog.write("intent", type(intent).__name__)
        if isinstance(intent, Speak):
            self.on_phase(PHASE_SPEAKING, intent.text)
            # Non-blocking: queue it and move on. The loop keeps listening the whole time, so
            # the moment the user speaks, the next heard line interrupts this (see run()).
            self.tts.say(intent.text)
            if self.session.phase.name == "LISTENING":
                self.on_phase(PHASE_LISTENING, "")

        elif isinstance(intent, ShowAnswer):
            self.bridge.show_answer()

        elif isinstance(intent, AnswerCard):
            self.bridge.answer_card(intent.ease)
            tracelog.write("answer_card", f"ease={intent.ease}")

        elif isinstance(intent, StartOverrideTimer):
            self._override_deadline = time.monotonic() + intent.seconds

        elif isinstance(intent, UndoCard):
            if self.bridge.undo():
                self._override_deadline = None
                self.on_phase(PHASE_AWAITING, "")
            else:
                self.on_error("Anki had nothing to undo.")

        elif isinstance(intent, RegradeCard):
            if not self.bridge.regrade(intent.card_id, intent.ease):
                self.on_error("Could not re-grade that card.")
            # The reviewer never moved, so the user is still on an unanswered card.
            self._reattach_current()

        elif isinstance(intent, FlagCard):
            ok = self.bridge.set_flag(intent.flag)
            tracelog.write("flag", f"flag={intent.flag} ok={ok}")
            if not ok:
                self.on_error("Could not flag this card.")

        elif isinstance(intent, BuryCard):
            ok = self.bridge.bury_current()
            tracelog.write("bury", f"ok={ok}")
            if not ok:
                self.on_error("Could not skip this card.")

        elif isinstance(intent, NextCard):
            self._override_deadline = None
            pending.extend(self._advance())

        elif isinstance(intent, Quit):
            self._stopping.set()
            return False

    def _reattach_current(self) -> None:
        """Point the session at the card the reviewer is showing, without re-reading it."""
        try:
            card = self.bridge.current_card()
        except BridgeError:
            return
        self._last_card_id = card.card_id
        self.on_card(card.question)
        self.session.resume_card(card)

    def _advance(self) -> list:
        # Answering is asynchronous; without waiting for the reviewer to present a fresh
        # question we read back the card just answered and speak it again.
        if not self.bridge.wait_for_question():
            self.tts.say("Deck finished")
            self._stopping.set()
            return []
        try:
            card = self.bridge.current_card()
        except NoCardShowing:
            self.tts.say("Deck finished")
            self._stopping.set()
            return []
        except BridgeError as exc:
            self.on_error(str(exc))
            self._stopping.set()
            return []

        repeat = card.card_id == self._last_card_id
        self._last_card_id = card.card_id
        self.on_card(card.question)
        intents = self.session.begin_card(card)
        if repeat:
            # A lapsed card really can come straight back. Say so, otherwise it is
            # indistinguishable from the bug where the loop re-read a stale card.
            return [Speak("Again")] + intents
        return intents

    # --- main loop ---

    def run(self) -> None:
        try:
            self.on_phase(PHASE_LOADING, "Starting whisper…")
            # State the settings actually in force. Anki keeps a user's existing add-on config
            # across updates, so a stale value can silently override a new default and make a
            # feature look broken when it was simply never switched on.
            tracelog.reset()
            tracelog.write(
                "config",
                f"grading={self.cfg.grading_mode} pause={self.cfg.override_window_s} "
                f"flag_on_skip={self.cfg.flag_on_skip} terminator={self.cfg.terminator!r}",
            )
            self.on_heard(
                f"[settings] grading={self.cfg.grading_mode} "
                f"pause={self.cfg.override_window_s}s flag_on_skip={self.cfg.flag_on_skip} "
                f"vad={self.cfg.vad_threshold} threads={self.cfg.whisper_threads}"
            )
            self.stt.preflight()
            self.tts.preflight()
            card = self.bridge.preflight()
        except (TranscriberError, SpeakerError, BridgeError) as exc:
            self.on_error(str(exc))
            self.on_finished("")
            return

        # Warm the judge while whisper is loading, so the first card that needs it does not
        # pay for a cold model.
        threading.Thread(target=prewarm, args=(self.cfg,), daemon=True).start()

        try:
            self.stt.start()
        except Exception as exc:  # noqa: BLE001 - surfaced in the dialog, not a traceback
            self.on_error(f"Could not start whisper-stream: {exc}")
            self.on_finished("")
            return

        try:
            self.on_card(card.question)
            self._execute(self.session.begin_card(card))

            while not self._stopping.is_set():
                line = self.stt.get(timeout=POLL_S)
                if line:
                    # The user is speaking, so stop talking over them immediately. This is the
                    # whole of barge-in: sensing is never gated on whether the TTS is busy, and
                    # any heard input cuts the speech off at once. (Headphones required, so a
                    # heard line is always the user, never our own voice.)
                    self.tts.interrupt()
                    action = match_command(line, self.cfg.command_words, self.cfg.terminator)
                    tracelog.write(
                        "heard", f"{line!r} action={action} phase={self.session.phase.name}"
                    )
                    self.on_heard(f"{line}   [{action or 'answer'}]")
                    before = self.session.graded
                    if self.session.phase.name == "LISTENING":
                        self.on_phase(PHASE_GRADING, "")
                    t0 = time.monotonic()
                    produced = self.session.on_line(line)
                    # on_line is cheap except when it grades; timing it isolates the LLM cost so
                    # the trace shows exactly how much of the wait is grading vs whisper emission.
                    dt_ms = (time.monotonic() - t0) * 1000
                    tracelog.write(
                        "intents",
                        (", ".join(type(i).__name__ for i in produced) or "(none)")
                        + f"  decide={dt_ms:.0f}ms",
                    )
                    self._execute(produced)
                    if self.session.phase.name == "AWAITING_EASE":
                        self.on_phase(PHASE_AWAITING, "")
                    if self.session.graded > before and self.session.last_verdict:
                        verdict = self.session.last_verdict
                        self.on_verdict(
                            verdict.correct,
                            verdict.score,
                            verdict.source,
                            self.session.last_transcript,
                        )
                        self.on_phase(PHASE_VERDICT, "")
                    continue

                if not self.stt.is_alive():
                    # whisper-stream exits immediately when the microphone is denied, which is
                    # exactly what happens before the macOS permission prompt is answered.
                    # Without this the window sits on "Listening…" forever.
                    self.on_error(
                        "whisper-stream stopped. If macOS asked for microphone access, allow it "
                        "in System Settings → Privacy & Security → Microphone (grant it to Anki), "
                        "then press Start again."
                    )
                    break

                if (
                    self._override_deadline
                    and time.monotonic() >= self._override_deadline
                    and not self.tts.is_busy
                ):
                    # Deadline reached, but not while the answer is still being read — a pause of
                    # 0 with answer-reading on means "advance the instant the read finishes".
                    self._override_deadline = None
                    self._execute(self.session.on_override_expired())
        except Exception as exc:  # noqa: BLE001 - a crashed worker must not take Anki with it
            log.exception("voice worker failed")
            self.on_error(str(exc))
        finally:
            tracelog.write("duplicate-lines-suppressed", str(self.stt.suppressed))
            self.stt.stop()
            self.on_phase(PHASE_STOPPED, "")
            self.on_finished(self._summary())

    def _summary(self) -> str:
        graded = self.session.graded
        if not graded:
            return "No cards graded."
        return (
            f"Reviewed {graded} card{'s' if graded != 1 else ''} — "
            f"{self.session.correct} correct, {graded - self.session.correct} incorrect."
        )
