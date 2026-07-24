"""Talks to Anki from inside Anki.

The add-on runs in Anki's own process, so there is no AnkiConnect and no HTTP — it drives
`mw.reviewer` directly. The calls used here (`reviewer._showAnswer`, `reviewer._answerCard`)
are the same private methods AnkiConnect itself uses to implement `guiShowAnswer` and
`guiAnswerCard`, checked against the installed AnkiConnect source for Anki 25.02.5. They are
private but universally relied upon by add-ons; the public API has no equivalent.

**Everything here must run on the GUI thread.** The voice loop lives on a worker thread, so
each method marshals onto the main thread and blocks until it has an answer. Touching the
collection from a background thread corrupts state in ways that surface much later.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from aqt import mw

from .avr.cards import Card

NO_CARD_ADVICE = (
    "No card is showing. Open a deck and click Study Now so a card is up in the reviewer, "
    "then start voice review."
)


class BridgeError(RuntimeError):
    pass


class NoCardShowing(BridgeError):
    pass


def run_on_main(fn: Callable[[], Any], timeout: float = 10.0) -> Any:
    """Run `fn` on the GUI thread and return its result, re-raising anything it throws."""
    if threading.current_thread() is threading.main_thread():
        return fn()

    done = threading.Event()
    box: dict[str, Any] = {}

    def wrapper() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - ferried to the calling thread below
            box["error"] = exc
        finally:
            done.set()

    mw.taskman.run_on_main(wrapper)
    if not done.wait(timeout):
        raise BridgeError("Anki did not respond on the GUI thread in time")
    if "error" in box:
        raise box["error"]
    return box.get("value")


class AnkiBridge:
    """Same surface the CLI's AnkiConnect client exposes, so the runner does not care which."""

    def _reviewer(self) -> Any:
        reviewer = getattr(mw, "reviewer", None)
        if reviewer is None:
            raise BridgeError("Anki has no reviewer available")
        return reviewer

    def _review_active(self) -> bool:
        try:
            return self._reviewer().card is not None and mw.state == "review"
        except BridgeError:
            return False

    def is_reviewing(self) -> bool:
        return bool(run_on_main(self._review_active))

    def current_card(self) -> Card:
        def read() -> Card:
            if not self._review_active():
                raise NoCardShowing(NO_CARD_ADVICE)
            card = self._reviewer().card
            return Card.from_html(card.id, card.question(), card.answer())

        return run_on_main(read)

    def show_answer(self) -> None:
        def show() -> None:
            reviewer = self._reviewer()
            # Only meaningful while the question is up; calling it twice would advance nothing
            # but does log noise.
            if self._review_active() and reviewer.state == "question":
                reviewer._showAnswer()

        run_on_main(show)

    def answer_card(self, ease: int) -> None:
        if ease not in (1, 2, 3, 4):
            raise ValueError(f"ease must be 1-4, got {ease}")

        def answer() -> None:
            reviewer = self._reviewer()
            if not self._review_active():
                raise NoCardShowing(NO_CARD_ADVICE)
            if reviewer.state != "answer":
                # Anki refuses to grade a card whose answer is not shown, and silently doing
                # nothing here would look like a dropped grade.
                reviewer._showAnswer()
            buttons = mw.col.sched.answerButtons(reviewer.card)
            reviewer._answerCard(min(ease, buttons))

        run_on_main(answer)

    def bury_current(self) -> bool:
        """Set the current card aside for this session, without grading it.

        This is what "skip" needs. The reviewer only advances when a card is answered, and
        answering is a grade — so without burying, skip landed back on the same card and read
        it again. Bury is the reviewer's own `-` shortcut.

        Prefers the reviewer's method when present and falls back to the scheduler directly,
        since this is the one call whose exact name has moved between Anki versions.
        """

        def bury() -> bool:
            if not self._review_active():
                return False
            reviewer = self._reviewer()
            card = reviewer.card
            method = getattr(reviewer, "bury_current_card", None)
            if callable(method):
                method()
                return True
            mw.col.sched.bury_cards([card.id])
            mw.reset()  # refresh the reviewer so it moves off the buried card
            return True

        return bool(run_on_main(bury))

    def reviewer_state(self) -> Optional[str]:
        """'question', 'answer', or None when not reviewing."""

        def read() -> Optional[str]:
            if not self._review_active():
                return None
            return getattr(self._reviewer(), "state", None)

        return run_on_main(read)

    def wait_for_question(self, timeout: float = 5.0, poll: float = 0.03) -> bool:
        """Block until the reviewer is showing a fresh question.

        Answering is asynchronous — in Anki 25.x `_answerCard` goes through an undoable
        operation, so it returns before the next card is on screen. Reading the card straight
        afterwards hands back the one just answered, and the loop re-speaks and re-grades it.

        Waiting on the reviewer's own state is the correct signal rather than watching for the
        card id to change: a lapsed card genuinely comes back as the very next card, and an
        id-based wait cannot tell that apart from a stale read except by stalling until it
        times out.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.reviewer_state()
            if state is None:
                return False  # deck finished, or review was exited
            if state == "question":
                return True
            time.sleep(poll)
        return self.reviewer_state() == "question"

    def preflight(self) -> Card:
        return self.current_card()
