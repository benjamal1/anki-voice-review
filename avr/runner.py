"""Turns the session's intents into real I/O.

Deliberately thin. Every decision lives in `session.py`; this file only knows how to speak,
listen, and talk to Anki. If you find yourself adding an `if` here about *what* should happen,
it belongs in the state machine instead.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .anki import AnkiConnect, AnkiError, NoCardShowing
from .config import Config
from .grade import grade
from .session import (
    AnswerCard,
    BuryCard,
    FlagCard,
    Intent,
    NextCard,
    Quit,
    Session,
    ShowAnswer,
    Speak,
    StartOverrideTimer,
)

log = logging.getLogger(__name__)

# How long to block on the transcript queue per tick. Short enough that the override timer
# fires close to on time, long enough not to spin the CPU.
POLL_S = 0.15


@dataclass
class CardTiming:
    card_id: int
    seconds: float
    source: str


class Runner:
    def __init__(self, cfg: Config, anki: AnkiConnect, transcriber, speaker) -> None:
        self.cfg = cfg
        self.anki = anki
        self.stt = transcriber
        self.tts = speaker
        self.session = Session(cfg=cfg, grade_fn=grade)
        self.timings: list[CardTiming] = []
        self._override_deadline: float | None = None
        self._card_started = 0.0
        self._running = False
        self._skip_unsupported = False

    # --- intent execution ---

    def _execute(self, intents: list[Intent]) -> None:
        """Run intents in order. Intents that produce more intents are queued, not recursed."""
        pending = list(intents)
        while pending:
            intent = pending.pop(0)

            if isinstance(intent, Speak):
                # The gate: `say` blocks, then the transcript backlog is discarded, so whisper
                # never hands us our own synthesised voice as if the user had said it.
                self.tts.speak(intent.text, gate=self.stt)

            elif isinstance(intent, ShowAnswer):
                self.anki.show_answer()

            elif isinstance(intent, AnswerCard):
                self.anki.answer_card(intent.ease)
                self._record_timing()

            elif isinstance(intent, StartOverrideTimer):
                self._override_deadline = time.monotonic() + intent.seconds

            elif isinstance(intent, FlagCard):
                # AnkiConnect has no flag action either, same as bury. The add-on does this
                # through the collection; over HTTP it is simply not available.
                log.warning("flagging is not available over AnkiConnect; skipped")

            elif isinstance(intent, BuryCard):
                # AnkiConnect exposes no bury action (checked: none of its 121 actions), so
                # skip cannot work over HTTP. Say so rather than silently re-reading the card,
                # which is what the old behaviour amounted to. The add-on, running inside
                # Anki, has the scheduler API and does support it.
                self._skip_unsupported = True
                self.tts.speak("Skip is not available from the command line", gate=self.stt)

            elif isinstance(intent, NextCard):
                if self._skip_unsupported:
                    self._skip_unsupported = False
                    continue  # stay on this card; there is nothing to advance to
                self._override_deadline = None
                pending.extend(self._advance())

            elif isinstance(intent, Quit):
                self._running = False
                return

    def _wait_for_new_card(self, timeout: float = 3.0, poll: float = 0.05) -> None:
        """Give Anki a moment to present the next card before reading it.

        The add-on can watch `reviewer.state` directly; over AnkiConnect the card id is the
        only signal available, so a lapsed card that legitimately comes straight back will
        wait out the timeout. That is the cost of not being in-process.
        """
        previous = self.session.card.card_id if self.session.card else 0
        if not previous:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.anki.current_card().card_id != previous:
                    return
            except AnkiError:
                return
            time.sleep(poll)

    def _record_timing(self) -> None:
        if not self._card_started:
            return
        verdict = self.session.last_verdict
        self.timings.append(
            CardTiming(
                card_id=self.session.card.card_id if self.session.card else 0,
                seconds=time.monotonic() - self._card_started,
                source=verdict.source if verdict else "manual",
            )
        )
        self._card_started = 0.0

    def _advance(self) -> list[Intent]:
        # guiAnswerCard returns before the reviewer has swapped in the next card, so reading
        # immediately hands back the card just answered and the loop re-speaks it.
        self._wait_for_new_card()
        try:
            card = self.anki.current_card()
        except NoCardShowing:
            self.tts.speak("Deck finished", gate=self.stt)
            self._running = False
            return []
        except AnkiError as exc:
            log.error("lost Anki: %s", exc)
            self._running = False
            return []

        self._card_started = time.monotonic()
        return self.session.begin_card(card)

    # --- main loop ---

    def run(self) -> None:
        card = self.anki.preflight()
        self.stt.start()
        self._running = True
        self._card_started = time.monotonic()

        try:
            self._execute(self.session.begin_card(card))
            while self._running:
                line = self.stt.get(timeout=POLL_S)

                if line:
                    log.debug("heard: %s", line)
                    self._execute(self.session.on_line(line))
                    continue

                if self._override_deadline and time.monotonic() >= self._override_deadline:
                    self._override_deadline = None
                    self._execute(self.session.on_override_expired())
        except KeyboardInterrupt:
            print()  # keep the summary off the ^C line
        finally:
            self.stt.stop()

    # --- reporting ---

    def summary(self) -> str:
        graded = self.session.graded
        if not graded:
            return "No cards graded."

        lines = [
            "",
            f"Reviewed {graded} card(s) — {self.session.correct} correct, "
            f"{graded - self.session.correct} incorrect.",
        ]
        if self.timings:
            times = [t.seconds for t in self.timings]
            judged = sum(1 for t in self.timings if t.source == "judge")
            lines.append(
                f"Per-card: median {sorted(times)[len(times) // 2]:.1f}s, "
                f"slowest {max(times):.1f}s, fastest {min(times):.1f}s."
            )
            lines.append(
                f"The LLM judge was needed on {judged}/{len(self.timings)} card(s); "
                "the rest were settled by fuzzy match alone."
            )
        return "\n".join(lines)
