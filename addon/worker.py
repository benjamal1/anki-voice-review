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
from .avr.grade import grade
from .avr.session import (
    AnswerCard,
    BuryCard,
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

log = logging.getLogger(__name__)

POLL_S = 0.15

# Phases reported to the UI. Strings rather than an enum so the dialog stays dumb.
PHASE_LOADING = "loading"
PHASE_SPEAKING = "speaking"
PHASE_LISTENING = "listening"
PHASE_GRADING = "grading"
PHASE_VERDICT = "verdict"
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
        self.stt = Transcriber(cfg.whisper_bin, cfg.whisper_model)
        self.tts = Speaker(cfg.say_voice, cfg.say_rate, cfg.echo_tail_s)

        self._stop = threading.Event()
        self._override_deadline: Optional[float] = None
        self._last_card_id = 0

    def request_stop(self) -> None:
        """Stop now, not at the end of the current sentence.

        Interrupting the speaker matters twice over: it is what the user asked for when they
        pressed Stop, and the worker is otherwise blocked inside `say` and cannot reach the
        code that releases the microphone — so a restart would find whisper still holding it
        and the new process would exit immediately.
        """
        self._stop.set()
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
        while pending and not self._stop.is_set():
            intent = pending.pop(0)

            if isinstance(intent, Speak):
                self.on_phase(PHASE_SPEAKING, intent.text)
                # Blocking, then draining: the mic is open the whole time, so this is what
                # stops whisper transcribing Anki's own voice back as the user's answer.
                self.tts.speak(intent.text, gate=self.stt)
                if self.session.phase.name == "LISTENING":
                    self.on_phase(PHASE_LISTENING, "")

            elif isinstance(intent, ShowAnswer):
                self.bridge.show_answer()

            elif isinstance(intent, AnswerCard):
                self.bridge.answer_card(intent.ease)

            elif isinstance(intent, StartOverrideTimer):
                self._override_deadline = time.monotonic() + intent.seconds

            elif isinstance(intent, BuryCard):
                if not self.bridge.bury_current():
                    self.on_error("Could not skip this card.")

            elif isinstance(intent, NextCard):
                self._override_deadline = None
                pending.extend(self._advance())

            elif isinstance(intent, Quit):
                self._stop.set()
                return

    def _advance(self) -> list:
        # Answering is asynchronous; without waiting for the reviewer to present a fresh
        # question we read back the card just answered and speak it again.
        if not self.bridge.wait_for_question():
            self.tts.speak("Deck finished", gate=self.stt)
            self._stop.set()
            return []
        try:
            card = self.bridge.current_card()
        except NoCardShowing:
            self.tts.speak("Deck finished", gate=self.stt)
            self._stop.set()
            return []
        except BridgeError as exc:
            self.on_error(str(exc))
            self._stop.set()
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
            self.stt.preflight()
            self.tts.preflight()
            card = self.bridge.preflight()
        except (TranscriberError, SpeakerError, BridgeError) as exc:
            self.on_error(str(exc))
            self.on_finished("")
            return

        try:
            self.stt.start()
        except Exception as exc:  # noqa: BLE001 - surfaced in the dialog, not a traceback
            self.on_error(f"Could not start whisper-stream: {exc}")
            self.on_finished("")
            return

        try:
            self.on_card(card.question)
            self._execute(self.session.begin_card(card))

            while not self._stop.is_set():
                line = self.stt.get(timeout=POLL_S)

                if line:
                    self.on_heard(line)
                    before = self.session.graded
                    if self.session.phase.name == "LISTENING":
                        self.on_phase(PHASE_GRADING, "")
                    self._execute(self.session.on_line(line))
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

                if self._override_deadline and time.monotonic() >= self._override_deadline:
                    self._override_deadline = None
                    self._execute(self.session.on_override_expired())
        except Exception as exc:  # noqa: BLE001 - a crashed worker must not take Anki with it
            log.exception("voice worker failed")
            self.on_error(str(exc))
        finally:
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
