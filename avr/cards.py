"""Extract the grading target from a rendered Anki card.

`guiCurrentCard` hands back rendered HTML for both sides. Two shapes matter:

* **Cloze** — the revealed deletion sits inside ``span.cloze``. That span *is* the answer;
  the rest of the sentence was already read aloud as the question.
* **Basic** — Anki renders front, then ``<hr id=answer>``, then back. The back is the answer.

Everything here is a pure function over the two HTML strings, so it is testable without Anki.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

ANSWER_SEPARATOR = re.compile(r"<hr\s+id=[\"']?answer[\"']?\s*/?>", re.IGNORECASE)
SOUND_TAG = re.compile(r"\[sound:[^\]]*\]", re.IGNORECASE)
# Anki wraps the revealed deletion in <span class="cloze">. The class attribute may carry
# extra classes (cloze-inactive on sibling deletions), so match the token, not the whole value.
CLOZE_SPAN = re.compile(r'class=["\'][^"\']*\bcloze\b[^"\']*["\']', re.IGNORECASE)

# Block-level tags whose boundaries are real word separators. Without this, "<div>a</div><div>b</div>"
# collapses to "ab" and grading silently compares the wrong string.
_BLOCK_TAGS = {"br", "div", "p", "li", "tr", "td", "th", "hr", "h1", "h2", "h3", "h4", "h5", "h6"}


class _TextExtractor(HTMLParser):
    """Collect visible text, optionally only from inside ``span.cloze``."""

    def __init__(self, cloze_only: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self._cloze_only = cloze_only
        self._depth = 0  # nesting depth inside a cloze span (0 == outside)
        self._span_stack: list[bool] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "span":
            is_cloze = any(
                name == "class" and value and "cloze" in value.split()
                for name, value in attrs
            )
            self._span_stack.append(is_cloze)
            if is_cloze:
                self._depth += 1
        if tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "span" and self._span_stack:
            if self._span_stack.pop():
                self._depth -= 1
        if tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._cloze_only and self._depth == 0:
            return
        self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def _visible_text(html: str, cloze_only: bool = False) -> str:
    parser = _TextExtractor(cloze_only=cloze_only)
    parser.feed(html)
    parser.close()
    return parser.text()


def is_cloze(answer_html: str) -> bool:
    """True when Anki rendered a cloze deletion into the answer side."""
    return bool(CLOZE_SPAN.search(answer_html))


def strip_html(html: str) -> str:
    """Visible text only: tags gone, media refs gone, whitespace collapsed."""
    text = SOUND_TAG.sub(" ", html)
    text = _visible_text(text)
    text = text.replace(" ", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize(text: str) -> str:
    """Fold to the form used for comparison: lowercase, unaccented, punctuation-free.

    Grading compares what a person *said* against what a card *says*. Case, accents, and
    punctuation are all noise in that comparison, and the STT will not reproduce them
    reliably anyway.
    """
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def answer_text(question_html: str, answer_html: str) -> str:
    """The text a spoken answer should be graded against.

    Cloze cards grade against the deletion alone. Basic cards grade against the back field.
    Falls back to the whole answer side when neither shape is recognised, which is wrong-ish
    but still gradeable — better than returning nothing and silently marking every card wrong.
    """
    if is_cloze(answer_html):
        cloze_text = strip_html(_visible_text(SOUND_TAG.sub(" ", answer_html), cloze_only=True))
        if cloze_text:
            return cloze_text
        # A cloze card whose deletion rendered empty is malformed; fall through to the
        # back-field logic rather than returning "" and grading every answer wrong.

    parts = ANSWER_SEPARATOR.split(answer_html, maxsplit=1)
    if len(parts) == 2:
        return strip_html(parts[1])

    # No separator: Anki sometimes renders the answer standalone. Subtract the question text
    # if the answer is a strict superset of it, otherwise take the whole thing.
    answer = strip_html(answer_html)
    question = strip_html(question_html)
    if question and answer.startswith(question):
        remainder = answer[len(question):].strip()
        if remainder:
            return remainder
    return answer


@dataclass(frozen=True)
class Card:
    """A card reduced to the two strings the review loop cares about.

    Lives here rather than beside the AnkiConnect client so the session state machine — and
    the Anki add-on, which has no AnkiConnect at all — can use it without dragging in an HTTP
    client they will never call.
    """

    card_id: int
    question: str  # spoken aloud
    answer: str  # graded against
    raw_question_html: str = ""
    raw_answer_html: str = ""

    @classmethod
    def from_html(cls, card_id: int, question_html: str, answer_html: str) -> "Card":
        question_html = question_html or ""
        answer_html = answer_html or ""
        return cls(
            card_id=int(card_id or 0),
            question=strip_html(question_html),
            answer=answer_text(question_html, answer_html),
            raw_question_html=question_html,
            raw_answer_html=answer_html,
        )

    @classmethod
    def from_gui_current_card(cls, payload: dict) -> "Card":
        """Build from an AnkiConnect `guiCurrentCard` result."""
        return cls.from_html(
            payload.get("cardId", 0), payload.get("question", ""), payload.get("answer", "")
        )
