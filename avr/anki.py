"""AnkiConnect client.

Direct HTTP to localhost. Deliberately *not* the ``ssh mac curl`` transport used by the
sibling `anki-obsidian` project — that exists only because that tool runs on the OptiPlex.
This one runs on the Mac, alongside Anki, so the SSH hop would be pure latency.

The loop drives the GUI reviewer, so Anki must be running with a deck open.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .cards import answer_text, strip_html


NO_CARD_ADVICE = (
    "Anki is running, but no card is showing. Open a deck and click Study Now so a card is "
    "up in the reviewer, then run this again."
)


class AnkiError(RuntimeError):
    """AnkiConnect reachable but unhappy, or not reachable at all."""


class AnkiNotRunning(AnkiError):
    """Nothing is listening on the AnkiConnect port."""


class NoCardShowing(AnkiError):
    """Anki is running, but no card is up in the reviewer."""


@dataclass(frozen=True)
class Card:
    card_id: int
    question: str  # spoken aloud
    answer: str  # graded against
    raw_question_html: str
    raw_answer_html: str

    @classmethod
    def from_gui_current_card(cls, payload: dict[str, Any]) -> "Card":
        q_html = payload.get("question", "") or ""
        a_html = payload.get("answer", "") or ""
        return cls(
            card_id=int(payload.get("cardId", 0)),
            question=strip_html(q_html),
            answer=answer_text(q_html, a_html),
            raw_question_html=q_html,
            raw_answer_html=a_html,
        )


class AnkiConnect:
    def __init__(self, url: str, timeout: float = 5.0) -> None:
        self._url = url
        self._timeout = timeout

    def invoke(self, action: str, **params: Any) -> Any:
        body = json.dumps({"action": action, "version": 6, "params": params}).encode()
        request = urllib.request.Request(
            self._url, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.URLError as exc:
            raise AnkiNotRunning(
                f"Could not reach AnkiConnect at {self._url} ({exc.reason}). "
                "Open Anki on this Mac and make sure the AnkiConnect add-on is enabled."
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise AnkiNotRunning(f"AnkiConnect at {self._url} did not respond: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise AnkiError(f"AnkiConnect returned a non-JSON reply: {exc}") from exc

        if isinstance(payload, dict) and payload.get("error"):
            raise AnkiError(f"AnkiConnect rejected {action!r}: {payload['error']}")
        return payload.get("result") if isinstance(payload, dict) else payload

    # --- the three actions the loop actually uses ---

    def current_card(self) -> Card:
        try:
            result = self.invoke("guiCurrentCard")
        except AnkiError as exc:
            # AnkiConnect reports an idle reviewer as an error string, not a null result, so
            # the "open a deck" case arrives here rather than below. Both must land on the
            # same advice — the generic error text sends you looking for the wrong problem.
            if "review is not currently active" in str(exc).lower():
                raise NoCardShowing(NO_CARD_ADVICE) from exc
            raise
        if not result:
            raise NoCardShowing(NO_CARD_ADVICE)
        return Card.from_gui_current_card(result)

    def show_answer(self) -> None:
        self.invoke("guiShowAnswer")

    def answer_card(self, ease: int) -> None:
        if ease not in (1, 2, 3, 4):
            raise ValueError(f"ease must be 1-4, got {ease}")
        self.invoke("guiAnswerCard", ease=ease)

    def preflight(self) -> Card:
        """Prove the whole path works before the session starts talking to a wall.

        Distinguishes 'Anki is closed' from 'Anki is open but not reviewing', because the
        fix for each is different and a generic connection error sends you the wrong way.
        """
        self.invoke("version")  # raises AnkiNotRunning if the port is dead
        return self.current_card()  # raises NoCardShowing if the reviewer is idle
