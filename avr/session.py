"""The review loop's decision logic.

This is the project's primary test seam. It consumes transcript lines and timer events and
emits *intents* — it never touches a microphone, a speaker, Anki, or Ollama itself. That makes
the whole decision surface testable with plain strings, which is where the interesting bugs
live (command matching, override precedence, terminator handling).

The runner in `runner.py` is the thin part that turns intents into actual I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Union

from .cards import Card, normalize
from .config import EASE_AGAIN, EASE_BY_NAME, EASE_GOOD, Config
from .grade import Verdict

# "bury" is a synonym for "skip": both set the card aside without grading and without ever
# revealing the answer, which is what an image card or an unreadable card needs.
CONTROL_COMMANDS = frozenset({"repeat", "skip", "bury", "quit"})
EASE_COMMANDS = frozenset(EASE_BY_NAME)
ALL_COMMANDS = CONTROL_COMMANDS | EASE_COMMANDS


class Phase(Enum):
    LISTENING = auto()  # accumulating the spoken answer
    OVERRIDE = auto()  # verdict announced, briefly accepting a correction
    AWAITING_EASE = auto()  # answer read out, waiting indefinitely for the user to grade it
    FINISHED = auto()


# --- intents -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Speak:
    text: str


@dataclass(frozen=True)
class ShowAnswer:
    pass


@dataclass(frozen=True)
class AnswerCard:
    ease: int


@dataclass(frozen=True)
class NextCard:
    pass


@dataclass(frozen=True)
class BuryCard:
    """Set the current card aside for this session without grading it.

    Anki has no "skip": the reviewer only advances when a card is answered, and answering is
    a grade. Bury is the real primitive — it is what the reviewer's own `-` shortcut does.
    Without it, "skip" advanced to the card that was already showing and simply read it again.
    """


@dataclass(frozen=True)
class StartOverrideTimer:
    seconds: float


@dataclass(frozen=True)
class Quit:
    pass


# typing.Union, not `A | B`: this is a runtime expression, and Anki 25.02.5 bundles Python 3.9
# where `|` on classes raises TypeError. The add-on imports this module inside Anki.
Intent = Union[Speak, ShowAnswer, AnswerCard, NextCard, BuryCard, StartOverrideTimer, Quit]


def match_command(line: str) -> str | None:
    """Recognise a command **only as a whole utterance**.

    Substring matching would be a real bug, not a nicety: a card whose answer is
    "the again reflex" or "good cholesterol" would fire a grade mid-sentence and the user
    would never find out why their answer was cut off.
    """
    normalized = normalize(line)
    return normalized if normalized in ALL_COMMANDS else None


def split_terminator(line: str, terminator: str) -> tuple[str, bool]:
    """Split a line into (speech, terminated).

    Handles both "done" on its own and a trailing "...and that's Paris, done" — VAD often
    packages the whole answer and the terminator into one utterance, and requiring the user to
    pause before saying it would defeat the point.
    """
    normalized = normalize(line)
    term = normalize(terminator)
    if not term:
        return line, False
    if normalized == term:
        return "", True
    if normalized.endswith(" " + term):
        return normalized[: -(len(term) + 1)].strip(), True
    return line, False


# --- the machine ---------------------------------------------------------------------------


@dataclass
class Session:
    cfg: Config
    grade_fn: object  # (question, answer, transcript, cfg) -> Verdict

    phase: Phase = Phase.LISTENING
    card: Card | None = None
    buffer: list[str] = field(default_factory=list)
    pending_ease: int = EASE_GOOD
    last_verdict: Verdict | None = None
    last_transcript: str = ""
    graded: int = 0
    correct: int = 0

    # --- lifecycle ---

    def begin_card(self, card: Card) -> list[Intent]:
        self.card = card
        self.buffer = []
        self.phase = Phase.LISTENING
        self.last_verdict = None
        self.last_transcript = ""
        return [Speak(card.question)]

    # --- events ---

    def on_line(self, line: str) -> list[Intent]:
        if self.phase is Phase.FINISHED or not line.strip():
            return []
        if self.phase is Phase.OVERRIDE:
            return self._on_line_override(line)
        if self.phase is Phase.AWAITING_EASE:
            return self._on_line_awaiting_ease(line)
        return self._on_line_listening(line)

    def on_override_expired(self) -> list[Intent]:
        """Nobody objected inside the window, so the graded default stands."""
        if self.phase is not Phase.OVERRIDE:
            return []
        self.phase = Phase.LISTENING
        return [AnswerCard(self.pending_ease), NextCard()]

    # --- internals ---

    def _on_line_listening(self, line: str) -> list[Intent]:
        command = match_command(line)
        if command:
            return self._run_command(command)

        speech, terminated = split_terminator(line, self.cfg.terminator)
        if speech:
            self.buffer.append(speech)
        if terminated:
            return self._grade()
        return []

    def _on_line_awaiting_ease(self, line: str) -> list[Intent]:
        """Manual grading: nothing is decided until the user says so.

        No timer and no default. The whole point is that no automatic verdict is being trusted,
        so falling back to one after a few seconds would defeat it.
        """
        command = match_command(line)
        if command in EASE_COMMANDS:
            ease = EASE_BY_NAME[command]
            self.phase = Phase.LISTENING
            self.graded += 1
            if ease >= EASE_GOOD:
                self.correct += 1
            return [AnswerCard(ease), NextCard()]
        if command == "quit":
            self.phase = Phase.FINISHED
            return [Speak("Goodbye"), Quit()]
        if command in ("skip", "bury"):
            self.phase = Phase.LISTENING
            return [Speak("Skipping"), BuryCard(), NextCard()]
        if command == "repeat":
            # Re-read the answer, not the question — the answer is already on screen.
            return [Speak(self.card.answer if self.card else "")]
        return []

    def _on_line_override(self, line: str) -> list[Intent]:
        command = match_command(line)
        if command in EASE_COMMANDS:
            self.phase = Phase.LISTENING
            return [AnswerCard(EASE_BY_NAME[command]), NextCard()]
        if command == "quit":
            self.phase = Phase.FINISHED
            return [Quit()]
        # Anything else during the window is stray talk. Ignore rather than guess.
        return []

    def _run_command(self, command: str) -> list[Intent]:
        if command == "quit":
            self.phase = Phase.FINISHED
            return [Speak("Goodbye"), Quit()]

        if command in ("skip", "bury"):
            self.phase = Phase.LISTENING
            # No ShowAnswer anywhere on this path: an image card or an unreadable card should
            # be set aside without the answer ever being revealed or read out.
            # Bury first — without it the loop "advances" to the card already showing and
            # reads it again, which is what skip used to do.
            return [Speak("Skipping"), BuryCard(), NextCard()]

        if command == "repeat":
            # Restart the answer too — whatever was captured was against a card the user has
            # just admitted they did not properly hear.
            self.buffer = []
            question = self.card.question if self.card else ""
            return [Speak(question)]

        # A direct ease call skips grading entirely: the user has overruled it up front.
        ease = EASE_BY_NAME[command]
        self.phase = Phase.LISTENING
        self.graded += 1
        if ease >= EASE_GOOD:
            self.correct += 1
        return [ShowAnswer(), AnswerCard(ease), NextCard()]

    def _ask_user(self, preamble: str) -> list[Intent]:
        """Show and read the answer, then wait for the user to grade it themselves.

        Deliberately emits no StartOverrideTimer: there is no default to fall back to, so a
        timer would just reintroduce the guess this path exists to avoid.
        """
        assert self.card is not None
        self.phase = Phase.AWAITING_EASE
        intents: list[Intent] = [ShowAnswer()]
        if preamble:
            intents.append(Speak(preamble))
        intents.append(Speak(self.card.answer))
        intents.append(Speak("Good or again?"))
        return intents

    def _grade(self) -> list[Intent]:
        if self.card is None:
            return []

        transcript = " ".join(self.buffer).strip()
        self.last_transcript = transcript

        if self.cfg.manual:
            # Manual mode never grades. Read the answer out, then wait — indefinitely — for the
            # user to say how it went. No verdict is announced because none was formed.
            return self._ask_user("")

        if not transcript:
            # The terminator on its own means "I don't know" — the fastest way to mark a card
            # wrong without touching anything. Made explicit rather than falling out of
            # scoring silence at 0.0, so the intent is visible and the answer still gets read
            # back, which is the point of getting one wrong.
            #
            # This cannot be confused with a dead microphone: grading only runs when the
            # terminator is *heard*, so if nothing were being transcribed there would be no
            # terminator and no grading at all.
            verdict = Verdict(False, 0.0, "no answer", "nothing said before the end word")
        else:
            verdict = self.grade_fn(self.card.question, self.card.answer, transcript, self.cfg)

        self.last_verdict = verdict

        if getattr(verdict, "needs_human", False):
            # Similarity could not decide and no model was available to break the tie. Rather
            # than invent a verdict, ask. `verdict.correct` is meaningless here.
            return self._ask_user("I could not grade that.")

        self.graded += 1
        if verdict.correct:
            self.correct += 1

        self.pending_ease = EASE_GOOD if verdict.correct else EASE_AGAIN
        self.phase = Phase.OVERRIDE

        intents: list[Intent] = [ShowAnswer(), Speak("Correct" if verdict.correct else "Incorrect")]
        if not verdict.correct:
            # Hearing the right answer is the entire point of getting one wrong.
            intents.append(Speak(self.card.answer))
        intents.append(StartOverrideTimer(self.cfg.override_window_s))
        return intents
