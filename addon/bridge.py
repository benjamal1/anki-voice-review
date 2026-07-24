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
        # The GUI thread never ran our callback. Almost always means the main thread is blocked
        # by another add-on's hook. Naming it beats a silent stall.
        try:
            from . import tracelog

            tracelog.write("MAIN-THREAD-TIMEOUT", fn.__name__ if hasattr(fn, "__name__") else "?")
        except Exception:  # noqa: BLE001
            pass
        raise BridgeError("Anki did not respond on the GUI thread in time")
    if "error" in box:
        raise box["error"]
    return box.get("value")


class AnkiBridge:
    """Same surface the CLI's AnkiConnect client exposes, so the runner does not care which."""

    def __init__(self) -> None:
        # Anki tells us when a question appears rather than us polling for it. This is the
        # authoritative signal that the reviewer has moved on after an answer, which is what
        # the "it read me the same question again" bug came down to.
        self._question_shown = threading.Event()
        self._hook_installed = False
        self._install_hook()

    def _install_hook(self) -> None:
        try:
            from aqt import gui_hooks

            gui_hooks.reviewer_did_show_question.append(self._on_question_shown)
            self._hook_installed = True
        except Exception:  # noqa: BLE001 - polling fallback covers this
            self._hook_installed = False

    def _on_question_shown(self, card: Any = None) -> None:
        self._question_shown.set()

    def close(self) -> None:
        """Detach from Anki. Without this the hook outlives the dialog and leaks per session."""
        if not self._hook_installed:
            return
        try:
            from aqt import gui_hooks

            gui_hooks.reviewer_did_show_question.remove(self._on_question_shown)
        except Exception:  # noqa: BLE001 - nothing useful to do if it is already gone
            pass
        self._hook_installed = False

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

        # Clear before answering, so wait_for_question() waits for the question that follows
        # this answer rather than returning instantly on the one already showing.
        self._question_shown.clear()

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
                raise BridgeError("no card is showing, so there is nothing to skip")
            reviewer = self._reviewer()
            card = reviewer.card
            cid = card.id

            # Use the SYNCHRONOUS scheduler bury, not reviewer.bury_current_card(). The
            # reviewer method runs the bury as a background CollectionOp and returns before it
            # finishes, which raced the loop's own advance and showed "buried 0 cards". The
            # scheduler call blocks, returns a count, and cannot race.
            errors = []
            count = None
            sched = mw.col.sched
            method = getattr(sched, "bury_cards", None) or getattr(sched, "buryCards", None)
            if callable(method):
                try:
                    changes = method([cid])
                except TypeError:
                    changes = method([cid], True)  # older signature: manual= positional
                count = getattr(changes, "count", None)
            else:
                errors.append("no bury_cards on the scheduler")

            from . import tracelog

            tracelog.write("bury-detail", f"cid={cid} queue={card.queue} count={count}")

            # Advance the reviewer to the next card ourselves, since the sync bury does not.
            advanced = False
            for name in ("nextCard", "_getCard"):
                nxt = getattr(reviewer, name, None)
                if callable(nxt):
                    try:
                        nxt()
                        advanced = True
                        break
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{name}: {exc}")
            if not advanced:
                mw.reset()  # last resort: rebuild queues; reviewer re-fetches

            if count == 0:
                # Buried nothing — report it rather than pretend it worked.
                raise BridgeError(
                    "Anki buried 0 cards. Another add-on (AJT Mortician?) may be intercepting "
                    "bury, or the card is not in a buryable state."
                )
            if errors and count is None:
                raise BridgeError("could not bury the card — " + "; ".join(errors))
            return True

        return bool(run_on_main(bury))

    def set_flag(self, flag: int) -> bool:
        """Apply one of Anki's coloured flags to the current card.

        Must be called before burying — once the card is buried the reviewer has moved on and
        there is no current card left to flag.

        The exact API has moved between Anki versions, so try the reviewer's own method first
        (what Ctrl+1..7 calls) and fall back to the collection.
        """
        if not 0 <= flag <= 7:
            raise ValueError(f"flag must be 0-7, got {flag}")

        def apply() -> bool:
            if not self._review_active():
                return False
            reviewer = self._reviewer()
            card = reviewer.card
            method = getattr(reviewer, "set_flag_on_current_card", None)
            if callable(method):
                method(flag)
                return True
            setter = getattr(mw.col, "set_user_flag_for_cards", None)
            if callable(setter):
                setter(flag, [card.id])
                return True
            card.set_user_flag(flag)  # oldest path
            mw.col.update_card(card)
            return True

        return bool(run_on_main(apply))

    def undo(self) -> bool:
        """Take back the last answer, putting that card back in front of the user.

        This is what lets grading advance immediately instead of pausing on every card for an
        override window that is usually unused.
        """

        def revert() -> bool:
            undo = getattr(mw, "undo", None)
            if callable(undo):
                undo()
                return True
            legacy = getattr(mw.col, "undo", None)
            if callable(legacy):
                legacy()
                mw.reset()
                return True
            return False

        return bool(run_on_main(revert))

    def regrade(self, card_id: int, ease: int) -> bool:
        """Apply an ease to a specific card, bypassing the reviewer.

        Needed because undo reverts the scheduling but leaves the reviewer on the card it had
        already advanced to, so a correction cannot go through the reviewer without grading
        the wrong card.
        """
        if ease not in (1, 2, 3, 4):
            raise ValueError(f"ease must be 1-4, got {ease}")

        def apply() -> bool:
            card = mw.col.get_card(card_id)
            if card is None:
                return False
            mw.col.sched.answer_card(card, ease)
            mw.reset()
            return True

        return bool(run_on_main(apply))

    def reviewer_state(self) -> Optional[str]:
        """'question', 'answer', or None when not reviewing."""

        def read() -> Optional[str]:
            if not self._review_active():
                return None
            return getattr(self._reviewer(), "state", None)

        return run_on_main(read)

    def wait_for_question(self, timeout: float = 5.0, poll: float = 0.03) -> bool:
        """Block until the reviewer is showing a fresh question.

        Prefers Anki's own `reviewer_did_show_question` hook, which fires exactly when a new
        question appears. Falls back to polling the reviewer's state, since the hook does not
        fire on every path (burying refreshes without a fresh question event).

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
            if self._question_shown.wait(poll):
                self._question_shown.clear()
                return True
            state = self.reviewer_state()
            if state is None:
                return False  # deck finished, or review was exited
            if state == "question":
                return True
        return self.reviewer_state() == "question"

    def preflight(self) -> Card:
        return self.current_card()
