# T1 — Anki bridge + card extraction

**Blocked by:** none
**Status:** todo

## Delivers

`avr peek` connects to AnkiConnect on the Mac, reads the live reviewer card, and prints the
extracted grading target. Works for both a Basic and a Cloze card.

## Scope

- AnkiConnect client: `POST http://127.0.0.1:8765`, actions `guiCurrentCard`, `guiShowAnswer`,
  `guiAnswerCard`. Direct HTTP, **not** the `ssh mac curl` transport used by `anki-obsidian`.
- Preflight that distinguishes three failure states with distinct messages: AnkiConnect
  unreachable (Anki not running), reachable but `guiCurrentCard` returns null (Anki running,
  not in the reviewer), and reachable with a card (ready).
- Card text extraction as a **pure function** over the rendered `question` / `answer` HTML:
  - Cloze: concatenated text of `span.cloze` elements.
  - Basic: text after the `<hr id=answer>` separator.
  - Both: strip tags, drop `[sound:...]` and media refs, collapse whitespace, normalise case
    and punctuation.

## Acceptance criteria

1. With Anki closed, `avr peek` exits non-zero with a message naming Anki as not running.
2. With Anki open but no deck in the reviewer, `avr peek` exits non-zero with a message saying
   so — distinct from the above.
3. With a Basic card showing, `avr peek` prints the back-field text with no HTML.
4. With a Cloze card showing, `avr peek` prints only the deletion text, not the whole sentence.
5. Extraction is unit-tested against captured real `guiCurrentCard` HTML for both note types,
   with no network involved.
