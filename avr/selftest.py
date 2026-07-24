"""End-to-end test against a live Anki, with the microphone replaced by a script.

Unit tests cover the decision logic; this covers everything the microphone was hiding — real
AnkiConnect calls, real card HTML from real note types, the asynchronous card advance that
produced the "it read me the same question again" bug, and the real Ollama judge.

Everything happens in a **throwaway deck** this module creates and deletes. It never touches
an existing card, and the scheduling it writes belongs entirely to notes it made itself.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Optional

from .anki import AnkiConnect, AnkiError
from .cards import Card
from .config import EASE_AGAIN, EASE_GOOD, Config
from .runner import Runner
from .stt import FakeTranscriber
from .tts import FakeSpeaker

DECK = "AVR Selftest (delete me)"


@dataclass
class Case:
    """One card, the lines that will be 'spoken', and what should happen."""

    label: str
    note_type: str
    fields: dict
    script: list
    expect_correct: Optional[bool]
    expect_ease: Optional[int]
    note: str = ""
    manual: bool = False
    forbid_show_answer: bool = False
    flag: int = 0


CASES = [
    Case(
        label="basic, exact answer",
        note_type="Basic",
        fields={"Front": "What is the capital of France?", "Back": "Paris"},
        script=["Paris", "done"],
        expect_correct=True,
        expect_ease=EASE_GOOD,
    ),
    Case(
        label="basic, answered in a full sentence",
        note_type="Basic",
        fields={"Front": "What is the largest planet?", "Back": "Jupiter"},
        script=["I think it is Jupiter", "done"],
        expect_correct=True,
        expect_ease=EASE_GOOD,
        note="whole-string similarity alone scores this ~0.4; containment must rescue it",
    ),
    Case(
        label="basic, clearly wrong",
        note_type="Basic",
        fields={"Front": "What is the boiling point of water?", "Back": "100 degrees Celsius"},
        script=["absolutely no idea", "done"],
        expect_correct=False,
        expect_ease=EASE_AGAIN,
    ),
    Case(
        label="cloze, deletion only",
        note_type="Cloze",
        fields={"Text": "The mitochondrion is the {{c1::powerhouse}} of the cell"},
        script=["the powerhouse", "done"],
        expect_correct=True,
        expect_ease=EASE_GOOD,
        note="must grade against the deletion, not the sentence it was just read",
    ),
    Case(
        label="cloze in MathJax, spoken as words",
        note_type="Cloze",
        fields={"Text": r"A K-bit image stores \(L = {{c1::2^K}}\) gray levels per pixel."},
        script=["2 to the power of K", "done"],
        expect_correct=True,
        expect_ease=EASE_GOOD,
        note="no span.cloze is emitted inside MathJax; notation must expand to spoken form",
    ),
    Case(
        label="spoken ease overrides the verdict",
        note_type="Basic",
        fields={"Front": "What is 2 plus 2?", "Back": "4"},
        script=["four", "done", "again"],
        expect_correct=True,
        expect_ease=EASE_AGAIN,
        note="graded correct, but the spoken override must win",
    ),
    Case(
        label="bare end word means I don't know",
        note_type="Basic",
        fields={"Front": "What is the atomic number of tungsten?", "Back": "74"},
        script=["done"],
        expect_correct=False,
        expect_ease=EASE_AGAIN,
        note="saying just the end word is the fastest hands-free way to mark a card wrong",
    ),
    Case(
        label="bury never reveals the answer",
        note_type="Basic",
        fields={"Front": "An image card you cannot read aloud", "Back": "secret"},
        script=["bury"],
        expect_correct=None,
        expect_ease=None,
        forbid_show_answer=True,
        note="image cards must be set aside without the answer being shown or spoken",
    ),
    Case(
        label="manual mode waits for the user to grade",
        note_type="Basic",
        fields={"Front": "What is the speed of light?", "Back": "300,000 km/s"},
        script=["something wrong done", "good"],
        expect_correct=None,
        expect_ease=EASE_GOOD,
        manual=True,
        note="answer is wrong, but manual mode must submit what the USER said, not a verdict",
    ),
    Case(
        label="skip advances without grading",
        note_type="Basic",
        fields={"Front": "Skip me", "Back": "nothing"},
        script=["skip"],
        expect_correct=None,
        expect_ease=None,
        note=(
            "AnkiConnect has no bury action, so skip cannot advance over HTTP; the add-on "
            "buries via the scheduler. This case asserts the CLI says so instead of silently "
            "re-reading the same card, which is what it used to do."
        ),
    ),
]


@dataclass
class Result:
    case: Case
    passed: bool
    detail: str
    seconds: float = 0.0


class ScriptedTranscriber(FakeTranscriber):
    """Feeds one card's lines, then goes quiet so the override window can expire."""

    def __init__(self) -> None:
        super().__init__(lines=[])
        self.pending: list = []

    def load(self, lines: list) -> None:
        self._lines = list(lines)

    def get(self, timeout: float):
        if not self._lines:
            time.sleep(min(timeout, 0.02))
            return None
        return self._lines.pop(0)


def build_deck(anki: AnkiConnect) -> list:
    """Create the throwaway deck and its notes. Returns the note ids actually added."""
    anki.invoke("createDeck", deck=DECK)

    models = set(anki.invoke("modelNames") or [])
    notes = []
    for case in CASES:
        model = case.note_type
        if model not in models:
            # Note type names are localised and vary between collections.
            candidates = [m for m in models if case.note_type.lower() in m.lower()]
            if not candidates:
                raise AnkiError(f"No note type resembling {case.note_type!r} in this collection")
            model = candidates[0]
        notes.append(
            {
                "deckName": DECK,
                "modelName": model,
                "fields": case.fields,
                "options": {"allowDuplicate": True},
                "tags": ["avr-selftest"],
            }
        )

    added = anki.invoke("addNotes", notes=notes)
    if not added or any(n is None for n in added):
        raise AnkiError(f"Anki refused some selftest notes: {added}")
    return added


def teardown(anki: AnkiConnect) -> None:
    """Remove the deck and every note in it. Best effort — never masks a test failure."""
    try:
        anki.invoke("deleteDecks", decks=[DECK], cardsToo=True)
    except AnkiError as exc:  # pragma: no cover - cleanup only
        print(f"  warning: could not delete {DECK!r}: {exc}")


def run(cfg: Config, keep: bool = False) -> int:
    anki = AnkiConnect(cfg.anki_url)

    print(f"Creating throwaway deck {DECK!r}…")
    build_deck(anki)
    print(f"  {len(CASES)} cards added\n")

    results: list = []
    try:
        anki.invoke("guiDeckReview", name=DECK)
        time.sleep(0.6)  # let the reviewer present the first card

        stt = ScriptedTranscriber()
        speaker = FakeSpeaker()

        for case in CASES:
            # Each case may need its own grading mode, so build a runner per case rather than
            # mutating a frozen Config.
            case_cfg = cfg
            if case.manual:
                case_cfg = replace(case_cfg, grading_mode="manual")
            if case.flag:
                case_cfg = replace(case_cfg, flag_on_skip=case.flag)
            runner = Runner(case_cfg, anki, stt, speaker)
            results.append(_run_case(runner, anki, stt, speaker, case))
    finally:
        if keep:
            print(f"\nLeaving {DECK!r} in place (--keep).")
        else:
            print(f"\nDeleting {DECK!r}…")
            teardown(anki)

    print()
    failures = [r for r in results if not r.passed]
    for result in results:
        mark = "pass" if result.passed else "FAIL"
        print(f"[{mark}] {result.case.label} ({result.seconds:.2f}s)")
        if result.detail:
            print(f"       {result.detail}")
        if result.case.note and not result.passed:
            print(f"       covers: {result.case.note}")

    print(f"\n{len(results) - len(failures)}/{len(results)} passed")
    return 1 if failures else 0


def _run_case(runner: Runner, anki: AnkiConnect, stt, speaker, case: Case) -> Result:
    started = time.monotonic()
    try:
        card = anki.current_card()
    except AnkiError as exc:
        return Result(case, False, f"no card showing: {exc}")

    before_id = card.card_id
    session = runner.session
    session.begin_card(card)
    graded_before = session.graded

    stt.load(case.script)
    speaker.said.clear()

    submitted: list = []
    revealed: list = []
    original_answer = anki.answer_card
    original_show = anki.show_answer

    def record_show() -> None:
        revealed.append(True)
        original_show()

    anki.show_answer = record_show  # type: ignore[method-assign]

    def record(ease: int) -> None:
        submitted.append(ease)
        original_answer(ease)

    anki.answer_card = record  # type: ignore[method-assign]
    try:
        while True:
            line = stt.get(timeout=0.05)
            if line is None:
                break
            runner._execute(session.on_line(line))

        # Nothing more will be said, so let the override window lapse.
        if session.phase.name == "OVERRIDE":
            runner._execute(session.on_override_expired())
    finally:
        anki.answer_card = original_answer  # type: ignore[method-assign]
        anki.show_answer = original_show  # type: ignore[method-assign]

    elapsed = time.monotonic() - started
    verdict = session.last_verdict

    problems = []
    if case.forbid_show_answer and revealed:
        problems.append("the answer was revealed on a card that should have been buried unseen")
    if case.expect_ease is None:
        if submitted:
            problems.append(f"expected no grade, but submitted ease {submitted}")
    else:
        if not submitted:
            problems.append("no grade was submitted")
        elif submitted[-1] != case.expect_ease:
            problems.append(f"expected ease {case.expect_ease}, got {submitted[-1]}")

    if case.manual and session.last_verdict is not None:
        if not getattr(session.last_verdict, "needs_human", False):
            problems.append("manual mode formed an automatic verdict")

    if case.expect_correct is not None:
        if session.graded == graded_before:
            problems.append("card was never graded")
        elif verdict and verdict.correct != case.expect_correct:
            problems.append(
                f"expected {'correct' if case.expect_correct else 'incorrect'}, "
                f"got the opposite (score {verdict.score:.2f}, via {verdict.source})"
            )

    # The bug that shipped: answering is asynchronous, so the loop could read back the card it
    # had just answered and speak it a second time. The first version of this check only
    # applied to graded cards, which is exactly why "skip" silently re-reading the same card
    # went unnoticed until the timing looked wrong.
    try:
        after = anki.current_card()
        if case.expect_ease is not None and after.card_id == before_id:
            problems.append("still on the same card after grading — stale-card race")
        if case.expect_ease is None:
            spoken = " ".join(speaker.said).lower()
            if after.card_id == before_id and "not available" not in spoken:
                problems.append(
                    "skip left the same card showing without saying it could not skip"
                )
    except AnkiError:
        pass  # deck exhausted, which is fine on the last case

    detail = "; ".join(problems)
    if not problems and verdict:
        detail = f"score {verdict.score:.2f} via {verdict.source}, ease {submitted[-1] if submitted else '-'}"
    return Result(case, not problems, detail, elapsed)
