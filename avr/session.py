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
from .config import (
    DEFAULT_COMMAND_WORDS,
    EASE_AGAIN,
    EASE_BY_NAME,
    EASE_GOOD,
    FLAG_NAMES,
    Config,
)
from .grade import Verdict

EASE_COMMANDS = frozenset(EASE_BY_NAME)


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
class UndoCard:
    """Revert the last grade in Anki's scheduler."""


@dataclass(frozen=True)
class RegradeCard:
    """Apply an ease to a specific card, not to whichever card the reviewer is showing.

    Undo reverts the scheduling but leaves the reviewer on the card it had already moved to —
    verified against a live collection. So re-grading has to name the card explicitly rather
    than going through the reviewer.
    """

    card_id: int
    ease: int


@dataclass(frozen=True)
class FlagCard:
    """Mark the card with one of Anki's coloured flags, so it can be found again later."""

    flag: int


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
Intent = Union[
    Speak,
    ShowAnswer,
    AnswerCard,
    NextCard,
    FlagCard,
    BuryCard,
    UndoCard,
    RegradeCard,
    StartOverrideTimer,
    Quit,
]


def match_command(line: str, words: dict | None = None, terminator: str = "") -> str | None:
    """Recognise a command, returning the action name ("skip", "good", ...) or None.

    Matched **only as a whole utterance**. Substring matching would be a real bug, not a
    nicety: a card whose answer is "the again reflex" or "good cholesterol" would fire a grade
    mid-sentence and the user would never find out why their answer was cut off.

    A trailing terminator is tolerated — saying "skip done" out of habit should still skip
    rather than being graded as the answer "skip".
    """
    words = words or DEFAULT_COMMAND_WORDS
    normalized = normalize(line)
    if terminator:
        term = normalize(terminator)
        if term and normalized.endswith(" " + term):
            normalized = normalized[: -(len(term) + 1)].strip()

    for action, spoken in words.items():
        if any(normalized == normalize(word) for word in spoken):
            return action
    return None


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
    previous_card: Card | None = None  # the card the last grade was applied to
    regrading: Card | None = None  # set while a grade is being taken back and redone
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

    def resume_card(self, card: Card) -> list[Intent]:
        """Attach to the card now showing without reading it out or changing phase.

        Used after an undo: the card comes back, but the user is mid-decision and does not
        need to hear the question again.
        """
        self.card = card
        self.buffer = []
        self.last_verdict = None
        self.last_transcript = ""
        return []

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

    def _command(self, line: str) -> str | None:
        return match_command(line, self.cfg.command_words, self.cfg.terminator)

    def _trailing_ease(self, line: str) -> str | None:
        """The ease action if the LAST spoken word is one, else None.

        In manual mode the answer content is never graded, so a line ending in an ease word is
        a grade said quickly — on its own ("good"), compound ("done good"), or straight after
        the answer ("proprioception good"). Only the last word is inspected, and only ease
        words (all single-word), so this never trips on an answer that merely contains one.
        """
        words = normalize(line).split()
        if not words:
            return None
        last = words[-1]
        for action in EASE_COMMANDS:
            if any(last == normalize(w) for w in self.cfg.command_words.get(action, [action])):
                return action
        return None

    def _grade_now(self, ease: int) -> list[Intent]:
        """Reveal the answer and grade it with `ease`, then advance. Manual fast path."""
        self.phase = Phase.LISTENING
        self.graded += 1
        if ease >= EASE_GOOD:
            self.correct += 1
        self.previous_card = self.card
        return [ShowAnswer(), AnswerCard(ease), NextCard()]

    def _on_line_listening(self, line: str) -> list[Intent]:
        command = self._command(line)
        if command:
            return self._run_command(command)

        if self.cfg.manual:
            # Grade the moment an ease word is heard, however it was packaged — no need to say
            # "done" first and then the grade on a separate breath.
            ease = self._trailing_ease(line)
            if ease is not None:
                return self._grade_now(EASE_BY_NAME[ease])

        speech, terminated = split_terminator(line, self.cfg.terminator)
        self._accumulate(speech)
        if terminated:
            return self._grade()
        return []

    def _accumulate(self, speech: str) -> None:
        """Add a heard fragment to the running answer, without duplicating.

        Everything said on the front of a card is bundled into one transcript, reset only when
        the card state changes (a new card, or the flip to the back). whisper sometimes repeats
        or extends its previous line rather than emitting a clean new utterance; appending those
        blindly gives the grader a garbled "the answer the answer is X" to judge. Collapsing an
        exact repeat or a prefix-extension keeps the bundled context clean.
        """
        speech = speech.strip()
        if not speech:
            return
        if self.buffer:
            prev_n, new_n = normalize(self.buffer[-1]), normalize(speech)
            if new_n == prev_n or new_n in ("", prev_n):
                return  # exact repeat
            if new_n.startswith(prev_n):
                self.buffer[-1] = speech  # whisper extended its previous line; replace
                return
            if prev_n.startswith(new_n):
                return  # a shorter repeat of what we already have
        self.buffer.append(speech)

    def _on_line_awaiting_ease(self, line: str) -> list[Intent]:
        """Manual grading: nothing is decided until the user says so.

        No timer and no default. The whole point is that no automatic verdict is being trusted,
        so falling back to one after a few seconds would defeat it.
        """
        command = self._command(line)
        if command is None:
            # Accept a grade even when it is not the whole utterance — "okay good", "that's
            # again". This is a grading phase, so a trailing ease word is unambiguous.
            command = self._trailing_ease(line)
        if command in EASE_COMMANDS:
            ease = EASE_BY_NAME[command]
            self.phase = Phase.LISTENING
            self.graded += 1
            if ease >= EASE_GOOD:
                self.correct += 1

            if self.regrading is not None:
                # Correcting a grade already given. The reviewer has moved on, so name the
                # card instead of grading whatever happens to be on screen — and do not
                # advance, because the card in front of the user has not been answered yet.
                card, self.regrading = self.regrading, None
                return [RegradeCard(card.card_id, ease)]

            self.previous_card = self.card
            return [AnswerCard(ease), NextCard()]
        if command == "quit":
            self.phase = Phase.FINISHED
            return [Speak("Goodbye"), Quit()]
        if command == "skip":
            return self._set_aside()
        if command == "undo":
            return self._undo()
        if command == "repeat":
            # Re-read the answer, not the question — the answer is already on screen.
            return [Speak(self.card.answer if self.card else "")]
        return []

    def _on_line_override(self, line: str) -> list[Intent]:
        command = self._command(line)
        if command in EASE_COMMANDS:
            self.phase = Phase.LISTENING
            self.previous_card = self.card
            return [AnswerCard(EASE_BY_NAME[command]), NextCard()]
        if command == "quit":
            self.phase = Phase.FINISHED
            return [Quit()]
        if command == "skip":
            # Previously only ease and quit were accepted here, so saying "skip" during the
            # window did nothing at all — silently, which is the worst way for it to fail.
            return self._set_aside()
        if command == "undo":
            return self._undo()
        if command == "repeat":
            return [Speak(self.card.question if self.card else "")]
        # Anything else during the window is stray talk. Ignore rather than guess.
        return []

    def _run_command(self, command: str) -> list[Intent]:
        if command == "undo":
            return self._undo()

        if command == "quit":
            self.phase = Phase.FINISHED
            return [Speak("Goodbye"), Quit()]

        if command == "skip":
            return self._set_aside()

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
        self.previous_card = self.card
        return [ShowAnswer(), AnswerCard(ease), NextCard()]

    def _undo(self) -> list[Intent]:
        """Take back the last grade.

        This is what makes advancing immediately after grading safe: rather than making every
        card wait out a window that is usually unused, the rare disagreement is corrected
        after the fact.
        """
        if not self.graded or self.previous_card is None:
            return [Speak("Nothing to undo")]
        self.graded -= 1
        if self.last_verdict is not None and self.last_verdict.correct:
            self.correct = max(0, self.correct - 1)

        self.regrading = self.previous_card
        self.previous_card = None

        # Wait for the grade rather than re-reading the card. You have just heard it and
        # decided you disagree with the verdict — being read the question again is noise, and
        # the only thing left to do is say how it should have been graded.
        self.phase = Phase.AWAITING_EASE

        # "Undone", not "done": the terminator word spoken into an open microphone invites
        # being transcribed straight back as a command, which in headphones mode it would be.
        return [Speak("Undone"), UndoCard()]

    def _set_aside(self) -> list[Intent]:
        """Skip/bury: put the card away without grading it and without revealing the answer.

        No ShowAnswer anywhere on this path — an image card, or anything that cannot be read
        aloud, should be set aside with the answer never shown or spoken.

        Flagging happens *before* burying, because once the card is buried the reviewer has
        moved on and there is no longer a current card to flag.
        """
        self.phase = Phase.LISTENING
        flag = self.cfg.flag_on_skip
        intents: list[Intent] = []
        if flag:
            colour = FLAG_NAMES.get(flag, str(flag))
            intents.append(Speak(f"Flagged {colour}"))
            intents.append(FlagCard(flag))
        else:
            intents.append(Speak("Skipping"))
        intents.append(BuryCard())
        intents.append(NextCard())
        return intents

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
        # Deliberately does not name the commands: a prompt containing the words it is asking
        # for gets transcribed back and competes with the reply.
        intents.append(Speak("Your call"))
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

        # Flip the card so the answer is visible on screen.
        intents: list[Intent] = [ShowAnswer()]
        if self.cfg.announce_verdict:
            intents.append(Speak("Correct" if verdict.correct else "Incorrect"))

        read = self.cfg.reads_answer(verdict.correct)
        if read:
            intents.append(Speak(self.card.answer))

        if not read and self.cfg.override_window_s <= 0:
            # Nothing to wait for: no answer being read and no configured pause. Straight on to
            # the next card. Making every card wait out an unused window costs more than the
            # rare disagreement, which "undo" fixes after the fact.
            self.phase = Phase.LISTENING
            self.previous_card = self.card
            intents.append(AnswerCard(self.pending_ease))
            intents.append(NextCard())
            return intents

        # Hold the advance. The override timer counts the configured pause, but the runner also
        # holds while the answer is still being spoken — so the card is not submitted out from
        # under a read in progress. A heard command barges in and cuts it short either way.
        self.phase = Phase.OVERRIDE
        intents.append(StartOverrideTimer(self.cfg.override_window_s))
        return intents
