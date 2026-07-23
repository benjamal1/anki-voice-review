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
from difflib import SequenceMatcher
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

# Tags whose *text content* is markup, not something anyone wrote on a card. Anki renders the
# note type's CSS into a <style> block inside the card HTML, so without this the grader reads
# ".card { font-size: 20px; text-align: center; }" aloud and grades answers against it.
_OPAQUE_TAGS = {"style", "script", "head", "title", "template"}

# Anki's own playback and TTS directives, which are instructions rather than content.
ANKI_DIRECTIVE = re.compile(r"\[anki:[^\]]*\]", re.IGNORECASE)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

# Add-ons inject status widgets into the card template. The FSRS Helper add-on adds
# <span id="FSRS_status">FSRS: enabled D: S: R:</span>, which is scheduler telemetry — not
# something anyone wrote on the card, and certainly not something to read aloud or grade
# against. Matched on id/class so a renamed or restyled widget is still caught.
#
# No \b around "fsrs": the real attribute is id="FSRS_status", and underscore is a word
# character, so \bfsrs\b does not match it. Plain substring is what actually catches this.
INJECTED_WIDGET = re.compile(r"fsrs|review_?(info|stats)|card_?stats", re.IGNORECASE)

# MathJax delimiters. Left in, `say` reads the backslashes; they carry no spoken meaning.
MATHJAX_DELIM = re.compile(r"\\[()\[\]]")

# Anki renders an unrevealed cloze deletion as this literal placeholder.
CLOZE_PLACEHOLDER = "[...]"


class _TextExtractor(HTMLParser):
    """Collect visible text, optionally only from inside ``span.cloze``."""

    def __init__(self, cloze_only: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self._cloze_only = cloze_only
        self._depth = 0  # nesting depth inside a cloze span (0 == outside)
        self._span_stack: list[bool] = []
        self._opaque_depth = 0  # nesting depth inside <style>/<script>/an injected widget
        self._opaque_tags: list[str] = []
        self.parts: list[str] = []

    @staticmethod
    def _is_injected_widget(attrs: list) -> bool:
        for name, value in attrs:
            if name in ("id", "class") and value and INJECTED_WIDGET.search(value):
                return True
        return False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _OPAQUE_TAGS or self._is_injected_widget(attrs):
            self._opaque_depth += 1
            self._opaque_tags.append(tag)
            return
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
        # An injected widget can be any tag, so close on the tag we actually opened rather
        # than on membership in _OPAQUE_TAGS — otherwise </span> would never close a skipped
        # <span id="FSRS_status"> and the rest of the card would be swallowed.
        if self._opaque_tags and self._opaque_tags[-1] == tag:
            self._opaque_tags.pop()
            self._opaque_depth = max(0, self._opaque_depth - 1)
            return
        if tag in _OPAQUE_TAGS:
            self._opaque_depth = max(0, self._opaque_depth - 1)
            return
        if tag == "span" and self._span_stack:
            if self._span_stack.pop():
                self._depth -= 1
        if tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._opaque_depth:
            return  # CSS or JS, not card content
        if self._cloze_only and self._depth == 0:
            return
        self.parts.append(data)

    def handle_comment(self, data: str) -> None:
        return  # never spoken

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
    """Visible text only: tags gone, styling gone, media refs gone, whitespace collapsed."""
    text = HTML_COMMENT.sub(" ", html)
    text = SOUND_TAG.sub(" ", text)
    text = ANKI_DIRECTIVE.sub(" ", text)
    text = _visible_text(text)
    text = MATHJAX_DELIM.sub(" ", text)
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


def speakable(text: str) -> str:
    """Make card text sound like a sentence rather than markup read out.

    Only applied to what gets spoken, never to what gets graded — the grader needs the literal
    placeholder to align the cloze diff.
    """
    # "[...]" read aloud is either silence or "bracket dot dot dot", neither of which tells you
    # a word is missing. "blank" is what a person reading the card to you would say.
    text = text.replace(CLOZE_PLACEHOLDER, " blank ")
    return re.sub(r"\s+", " ", text).strip()


def cloze_by_diff(question: str, answer: str) -> str:
    """Recover a cloze deletion by diffing the question against the answer.

    Anki normally wraps a revealed deletion in ``span.cloze``, but not always: when the
    deletion sits inside MathJax (``\\(L = {{c1::2^K}}\\)``) it is emitted as plain text so
    MathJax can typeset it, leaving no marker at all. Such a card then looks like a Basic card
    and gets graded against the entire sentence — including the Back Extra field — so almost
    any spoken answer scores as correct.

    The question still renders the gap as ``[...]``, so aligning the two sides and taking
    whatever replaced the placeholder recovers the real answer.

    Only *replacements* at the placeholder count. Text merely appended on the answer side is
    the Back Extra field, which is context rather than the thing being recalled.
    """
    question_words = question.split()
    answer_words = answer.split()
    if not any(CLOZE_PLACEHOLDER in word for word in question_words):
        return ""

    matcher = SequenceMatcher(None, question_words, answer_words)
    found = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace":
            continue
        if any(CLOZE_PLACEHOLDER in word for word in question_words[i1:i2]):
            found.append(_stop_at_sentence_end(answer_words[j1:j2]))
    return " ".join(part for part in found if part).strip()


def _stop_at_sentence_end(words: list[str]) -> str:
    """Trim a recovered deletion at the first sentence boundary.

    When the deletion is the last thing in the question, the diff has nothing after it to
    anchor against, so the placeholder and every trailing word — including the Back Extra
    field — collapse into a single replacement. A cloze deletion does not span a sentence
    boundary, so cutting at the first full stop recovers the intended span.
    """
    for index, word in enumerate(words):
        if word.endswith((".", "!", "?")) and index + 1 < len(words):
            return " ".join(words[: index + 1])
    return " ".join(words)


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

    # An unmarked cloze — MathJax and some templates emit the deletion as plain text.
    from_diff = cloze_by_diff(strip_html(question_html), strip_html(answer_html))
    if from_diff:
        return from_diff

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
        # Answer extraction needs the raw "[...]" to align the diff, so it runs before the
        # placeholder is turned into something speakable.
        answer = answer_text(question_html, answer_html)
        return cls(
            card_id=int(card_id or 0),
            question=speakable(strip_html(question_html)),
            answer=answer,
            raw_question_html=question_html,
            raw_answer_html=answer_html,
        )

    @classmethod
    def from_gui_current_card(cls, payload: dict) -> "Card":
        """Build from an AnkiConnect `guiCurrentCard` result."""
        return cls.from_html(
            payload.get("cardId", 0), payload.get("question", ""), payload.get("answer", "")
        )
