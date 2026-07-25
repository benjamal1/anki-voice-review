"""Drive the real add-on worker with a fake Anki, a scripted microphone and a silent speaker.

This is the layer that was invisible. The session state machine was provably correct — "skip"
yields a BuryCard — and the transcript pane proved the line arrived. What nothing covered was
whether the worker then actually *called* Anki, and whether a failure in that call surfaced or
vanished. Both of those are testable without Anki: the bridge is the only thing that needs it.
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import types
import zipfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _install_aqt_stub() -> None:
    if "aqt" in sys.modules:
        return
    aqt = types.ModuleType("aqt")
    qt = types.ModuleType("aqt.qt")
    qt.__getattr__ = lambda name: type(name, (), {"__init__": lambda self, *a, **k: None})
    hooks = types.ModuleType("aqt.gui_hooks")

    class _Hook(list):
        def remove(self, fn):
            if fn in self:
                super().remove(fn)

    hooks.__getattr__ = lambda name: hooks.__dict__.setdefault(name, _Hook())
    utils = types.ModuleType("aqt.utils")
    utils.showWarning = lambda *a, **k: None
    aqt.mw = None
    aqt.qt = qt
    aqt.gui_hooks = hooks
    sys.modules.update({"aqt": aqt, "aqt.qt": qt, "aqt.gui_hooks": hooks, "aqt.utils": utils})


_install_aqt_stub()


def _import_built_addon():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from build_addon import build

    archive = build()
    target = pathlib.Path(tempfile.mkdtemp()) / "anki_voice_review"
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(target)
    sys.path.insert(0, str(target.parent))
    from anki_voice_review import worker as worker_module
    from anki_voice_review.avr.cards import Card as _Card
    from anki_voice_review.avr.config import Config as _Config

    return worker_module, _Card, _Config


worker_module, Card, Config = _import_built_addon()


class FakeBridge:
    """Records what the worker asks Anki to do. Optionally fails a chosen call."""

    def __init__(self, cards=None, fail: str = "") -> None:
        self.cards = list(cards or [Card(1, "question one", "answer one")])
        self.index = 0
        self.fail = fail
        self.calls: list = []

    def _maybe_fail(self, name: str) -> None:
        if self.fail == name:
            raise RuntimeError(f"{name} exploded")

    def preflight(self):
        return self.current_card()

    def current_card(self):
        if self.index >= len(self.cards):
            from anki_voice_review.bridge import NoCardShowing

            raise NoCardShowing("deck finished")
        return self.cards[self.index]

    def show_answer(self) -> None:
        self.calls.append(("show_answer",))

    def answer_card(self, ease: int) -> None:
        self._maybe_fail("answer_card")
        self.calls.append(("answer_card", ease))
        self.index += 1

    def bury_current(self) -> bool:
        self._maybe_fail("bury_current")
        self.calls.append(("bury",))
        self.index += 1
        return True

    def set_flag(self, flag: int) -> bool:
        self._maybe_fail("set_flag")
        self.calls.append(("flag", flag))
        return True

    def undo(self) -> bool:
        self.calls.append(("undo",))
        return True

    def regrade(self, card_id: int, ease: int) -> bool:
        self._maybe_fail("regrade")
        self.calls.append(("regrade", card_id, ease))
        return True

    def wait_for_question(self, timeout: float = 5.0) -> bool:
        return self.index < len(self.cards)

    def reviewer_state(self):
        return "question"


def run_worker(script, cfg=None, bridge=None, cards=None):
    """Run the real worker over a scripted transcript. Returns (bridge, errors, spoken)."""
    from anki_voice_review.avr.stt import FakeTranscriber
    from anki_voice_review.avr.tts import FakeSpeaker

    cfg = cfg or Config()
    bridge = bridge or FakeBridge(cards=cards)
    errors: list = []
    spoken: list = []

    w = worker_module.VoiceWorker(
        cfg=cfg,
        bridge=bridge,
        on_phase=lambda *a: None,
        on_card=lambda *a: None,
        on_heard=lambda *a: None,
        on_verdict=lambda *a: None,
        on_error=errors.append,
        on_finished=lambda *a: None,
    )
    w.stt = FakeTranscriber(lines=list(script))
    w.tts = FakeSpeaker()
    w.tts.said = spoken

    # Drive the loop directly rather than starting the thread, so the test is deterministic.
    card = bridge.preflight()
    w._execute(w.session.begin_card(card))
    while True:
        line = w.stt.get(timeout=0.01)
        if line is None:
            break
        w._execute(w.session.on_line(line))
    return bridge, errors, spoken


class TestSkipReachesAnki:
    """"skip does nothing" needed this: the session was right, so the question was whether the
    worker actually called Anki."""

    def test_skip_buries_the_card(self):
        bridge, errors, _ = run_worker(["skip"])
        assert ("bury",) in bridge.calls
        assert errors == []

    def test_bury_does_the_same(self):
        bridge, _, _ = run_worker(["bury"])
        assert ("bury",) in bridge.calls

    def test_skip_never_reveals_the_answer(self):
        bridge, _, spoken = run_worker(["skip"])
        assert ("show_answer",) not in bridge.calls
        assert "answer one" not in spoken

    def test_skip_does_not_grade(self):
        bridge, _, _ = run_worker(["skip"])
        assert not [c for c in bridge.calls if c[0] == "answer_card"]

    def test_skip_flags_first_then_buries(self):
        # The user's real config had flag_on_skip=1, so this is the exact ordering that runs.
        bridge, errors, _ = run_worker(["skip"], cfg=Config(flag_on_skip=1))
        assert bridge.calls.index(("flag", 1)) < bridge.calls.index(("bury",))
        assert errors == []

    def test_a_failing_flag_still_lets_the_card_be_buried(self):
        # Before intents were isolated, a raise here killed the batch before the bury and the
        # whole thing looked like "skip does nothing".
        bridge, errors, _ = run_worker(["skip"], cfg=Config(flag_on_skip=1), bridge=FakeBridge(fail="set_flag"))
        assert ("bury",) in bridge.calls, "a failed flag must not swallow the skip"
        assert errors, "and the failure must be reported, not silent"

    def test_a_failing_bury_is_reported(self):
        _, errors, _ = run_worker(["skip"], bridge=FakeBridge(fail="bury_current"))
        assert any("Bury" in e or "bury" in e for e in errors)


class TestManualModeReachesAnki:
    def test_saying_again_grades_the_card(self):
        cfg = Config(grading_mode="manual")
        bridge, errors, _ = run_worker(["my attempt done", "again"], cfg=cfg)
        assert ("answer_card", 1) in bridge.calls
        assert errors == []

    def test_saying_good_grades_the_card(self):
        cfg = Config(grading_mode="manual")
        bridge, _, _ = run_worker(["my attempt done", "good"], cfg=cfg)
        assert ("answer_card", 3) in bridge.calls

    def test_it_reads_the_answer_and_grades_nothing_before_you_speak(self):
        cfg = Config(grading_mode="manual")
        bridge, _, spoken = run_worker(["my attempt done"], cfg=cfg)
        assert "answer one" in spoken
        assert not [c for c in bridge.calls if c[0] == "answer_card"]

    def test_a_wrong_spoken_answer_still_grades_as_the_user_says(self):
        cfg = Config(grading_mode="manual")
        bridge, _, _ = run_worker(["completely wrong done", "good"], cfg=cfg)
        assert ("answer_card", 3) in bridge.calls, "manual mode must not form its own verdict"


class TestAutomaticModeReachesAnki:
    def test_a_correct_answer_is_submitted(self):
        bridge, errors, _ = run_worker(["answer one done"])
        assert [c for c in bridge.calls if c[0] == "answer_card"]
        assert errors == []

    def test_it_advances_to_the_next_card(self):
        cards = [Card(1, "q one", "answer one"), Card(2, "q two", "answer two")]
        bridge, _, spoken = run_worker(["answer one done"], cards=cards)
        assert "q two" in spoken, "the next card should have been read out"


class TestUndoReachesAnki:
    def test_undo_then_again_regrades_the_original_card(self):
        cards = [Card(11, "q one", "answer one"), Card(22, "q two", "answer two")]
        bridge, errors, _ = run_worker(["answer one done", "undo", "again"], cards=cards)
        assert ("undo",) in bridge.calls
        assert ("regrade", 11, 1) in bridge.calls, "the correction must name the original card"
        assert errors == []


class TestQuit:
    def test_quit_stops_the_loop(self):
        bridge, _, _ = run_worker(["quit", "skip"])
        assert ("bury",) not in bridge.calls, "nothing should run after quit"


class TestStreamingDuplicateSuppression:
    """Streaming transcription (whisper_step_ms > 0) re-transcribes the trailing audio window
    on every tick until the utterance slides out of it, so whisper-stream can emit the exact
    same recognised line several times in a row for one thing the user said once. Reproduced
    and fixed at the transcriber boundary — FakeTranscriber applies the same duplicate filter
    the real Transcriber does, so this drives the actual worker with no Anki or microphone."""

    def test_a_repeated_terminated_line_grades_only_once(self):
        cards = [Card(1, "q one", "answer one"), Card(2, "q two", "answer two")]
        bridge, errors, _ = run_worker(
            ["answer one done", "answer one done", "answer one done"], cards=cards
        )
        graded = [c for c in bridge.calls if c[0] == "answer_card"]
        assert len(graded) == 1, f"a streaming re-emission must not grade twice, got {graded}"
        assert errors == []

    def test_a_repeat_does_not_cascade_into_the_next_card(self):
        # Before the fix, every repeat re-fired the grade against whatever card the session was
        # attached to at that instant — including the card the loop had already advanced to,
        # silently grading it sight-unseen. That is "stuck on the grading screen, skips ahead"
        # from the user's report, not a hang.
        cards = [Card(1, "q one", "answer one"), Card(2, "q two", "answer two"), Card(3, "q three", "answer three")]
        bridge, _, _ = run_worker(
            ["answer one done", "answer one done", "answer one done"], cards=cards
        )
        assert bridge.index == 1, "only the first card should have been consumed"
        assert bridge.calls.count(("show_answer",)) == 1, (
            "the next card's answer must never be shown before the user has spoken to it"
        )

    def test_a_repeated_command_only_fires_once(self):
        bridge, errors, _ = run_worker(["skip", "skip", "skip"])
        assert len([c for c in bridge.calls if c[0] == "bury"]) == 1
        assert errors == []

    def test_a_repeated_manual_grade_only_fires_once(self):
        cfg = Config(grading_mode="manual")
        bridge, errors, _ = run_worker(
            ["my attempt done", "good", "good", "good"], cfg=cfg
        )
        assert len([c for c in bridge.calls if c[0] == "answer_card"]) == 1
        assert errors == []

    def test_a_genuinely_later_repeat_still_works(self):
        # A real second utterance of the same word — the user saying "skip" again a few
        # seconds later, on a different card — is well outside the streaming re-emission window
        # and must still go through. FakeTranscriber's delay simulates real elapsed time between
        # lines, unlike the tight back-to-back loop the other tests in this class use.
        from anki_voice_review.avr.stt import FakeTranscriber
        from anki_voice_review.avr.tts import FakeSpeaker

        cards = [Card(1, "q one", "answer one"), Card(2, "q two", "answer two")]
        bridge = FakeBridge(cards=cards)
        w = worker_module.VoiceWorker(
            cfg=Config(), bridge=bridge, on_phase=lambda *a: None, on_card=lambda *a: None,
            on_heard=lambda *a: None, on_verdict=lambda *a: None, on_error=lambda *a: None,
            on_finished=lambda *a: None,
        )
        w.stt = FakeTranscriber(lines=["skip", "skip"], delay=3.0)
        w.tts = FakeSpeaker()
        card = bridge.preflight()
        w._execute(w.session.begin_card(card))
        while True:
            line = w.stt.get(timeout=0.01)
            if line is None:
                break
            w._execute(w.session.on_line(line))
        assert bridge.calls.count(("bury",)) == 2, "a genuine repeat 3s apart must not be suppressed"



class TestRealWhisperLinesReachAnki:
    def test_timestamped_skip_buries(self):
        bridge, errors, _ = run_worker(["[00:00:00.000 --> 00:00:04.000]   Skip."])
        assert ("bury",) in bridge.calls, "the real 'skip' line must bury"
        assert errors == []

    def test_timestamped_again_grades_in_manual_mode(self):
        cfg = Config(grading_mode="manual")
        bridge, errors, _ = run_worker(
            [
                "[00:00:00.000 --> 00:00:04.240]   Proprioception done.",
                "[00:00:00.000 --> 00:00:02.000]   again",
            ],
            cfg=cfg,
        )
        assert ("answer_card", 1) in bridge.calls, "the real 'again' line must grade"
        assert errors == []

    def test_timestamped_good_grades(self):
        cfg = Config(grading_mode="manual")
        bridge, _, _ = run_worker(
            [
                "[00:00:00.000 --> 00:00:03.000]   my answer done",
                "[00:00:00.000 --> 00:00:02.000]   Good.",
            ],
            cfg=cfg,
        )
        assert ("answer_card", 3) in bridge.calls

    def test_keyboard_noise_between_commands_is_ignored(self):
        bridge, _, _ = run_worker(
            [
                "[00:00:00.000 --> 00:00:03.000]   (keyboard clicking)",
                "[00:00:00.000 --> 00:00:04.000]   Skip.",
            ]
        )
        assert ("bury",) in bridge.calls

    def test_auto_mode_grades_a_real_answer_line(self):
        cards = [Card(1, "q", "Paris")]
        bridge, _, _ = run_worker(["[00:00:00.000 --> 00:00:03.000]   Paris done."], cards=cards)
        assert [c for c in bridge.calls if c[0] == "answer_card"]
