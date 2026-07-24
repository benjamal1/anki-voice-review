"""Grader — fuzzy scoring and band routing, with the LLM call stubbed."""

import io
import json
import urllib.error

import pytest

from avr import grade as grade_mod
from avr.config import Config
from avr.grade import SHORT_ANSWER_CORRECT, correct_threshold, fuzzy_score, grade


def _fake_urlopen(body: str):
    """Minimal stand-in for urlopen's context-manager response."""

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return lambda *a, **k: _Response(body.encode())


@pytest.fixture
def cfg():
    return Config()


@pytest.fixture
def no_judge(monkeypatch):
    """Judge unreachable — the Ollama-down path."""
    monkeypatch.setattr(grade_mod, "ask_judge", lambda *a, **k: None)


def stub_judge(monkeypatch, verdict):
    calls = []

    def fake(question, answer, transcript, cfg):
        calls.append((question, answer, transcript))
        return verdict

    monkeypatch.setattr(grade_mod, "ask_judge", fake)
    return calls


class TestFuzzyScore:
    def test_identical_scores_one(self):
        assert fuzzy_score("Paris", "Paris") == 1.0

    def test_ignores_case_and_punctuation(self):
        assert fuzzy_score("Paris!", "paris") == 1.0

    def test_unrelated_scores_low(self):
        assert fuzzy_score("Paris", "photosynthesis") < 0.4

    def test_empty_transcript_scores_zero(self):
        # Saying nothing must never grade as correct.
        assert fuzzy_score("Paris", "") == 0.0

    def test_answering_in_a_full_sentence_is_not_penalised(self):
        # Nobody says "Paris". They say "the capital is Paris". Whole-string similarity scores
        # that 0.40 — almost the clearly-wrong floor — so window matching carries it.
        assert fuzzy_score("Paris", "the capital is Paris") >= 0.9

    def test_padding_does_not_rescue_a_missing_answer(self):
        # The flip side: window matching must not mark everything correct just because the
        # transcript is long.
        assert fuzzy_score("Paris", "I really have no idea about this one at all") < 0.6

    def test_a_near_miss_inside_a_sentence_still_scores_high(self):
        assert fuzzy_score("mitochondria", "I think it is the mitochondria") >= 0.9

    def test_a_short_answer_does_not_match_an_unrelated_long_word(self):
        # Character-window matching scored "Paris" 0.40 against "photosynthesis" on shared
        # letters alone — indistinguishable from a real hit. Word boundaries fix that.
        assert fuzzy_score("Paris", "photosynthesis is a process") < 0.4

    def test_a_fragment_of_a_long_answer_is_not_enough(self):
        # Directional: saying the answer plus padding is correct, saying one word of a long
        # answer is not.
        assert fuzzy_score("the sinoatrial node in the right atrium", "the node") < 0.6


class TestBandRouting:
    def test_exact_match_is_correct_without_calling_the_judge(self, cfg, monkeypatch):
        calls = stub_judge(monkeypatch, True)
        verdict = grade("Capital of France?", "Paris", "Paris", cfg)
        assert verdict.correct
        assert verdict.source == "fuzzy"
        assert calls == [], "clear matches must not pay for a model call"

    def test_clearly_wrong_is_incorrect_without_calling_the_judge(self, cfg, monkeypatch):
        calls = stub_judge(monkeypatch, True)
        verdict = grade("Capital of France?", "Paris", "photosynthesis is a process", cfg)
        assert not verdict.correct
        assert verdict.source == "fuzzy"
        assert calls == []

    def test_ambiguous_band_reaches_the_judge(self, cfg, monkeypatch):
        calls = stub_judge(monkeypatch, True)
        answer = "the powerhouse of the cell"
        transcript = "it makes energy for the cell"
        score = fuzzy_score(answer, transcript)
        assert cfg.fuzzy_wrong <= score < cfg.fuzzy_correct, "fixture must sit in the band"

        verdict = grade("What is the mitochondrion?", answer, transcript, cfg)
        assert verdict.correct
        assert verdict.source == "judge"
        assert len(calls) == 1

    def test_judge_can_overrule_toward_incorrect(self, cfg, monkeypatch):
        stub_judge(monkeypatch, False)
        verdict = grade("q", "the powerhouse of the cell", "it makes energy for the cell", cfg)
        assert not verdict.correct
        assert verdict.source == "judge"


class TestFallbacks:
    def test_ollama_down_still_returns_a_verdict(self, cfg, no_judge):
        verdict = grade("q", "the powerhouse of the cell", "it makes energy for the cell", cfg)
        assert verdict.source == "fuzzy-only"

    def test_no_judge_splits_the_ambiguous_band_instead_of_failing_everything(self, cfg, no_judge):
        # This used to return "incorrect" for the whole band. With the judge unreachable that
        # marks every partial match wrong, which reads as "it grades everything incorrect".
        midpoint = (cfg.fuzzy_correct + cfg.fuzzy_wrong) / 2
        upper = grade("q", "the powerhouse of the cell", "it makes energy for the cell", cfg)
        assert cfg.fuzzy_wrong <= upper.score < cfg.fuzzy_correct, "fixture must sit in the band"
        assert upper.score >= midpoint and upper.correct

        lower = grade("q", "the sinoatrial node of the heart", "something about the heart", cfg)
        if lower.score < midpoint:
            assert not lower.correct

    def test_judge_disabled_uses_the_same_split(self):
        cfg = Config(use_judge=False)
        # 0.556 sits inside the band and above its midpoint, so it should be given the
        # benefit of the doubt rather than marked wrong for want of a model.
        verdict = grade("q", "the powerhouse of the cell", "it makes energy for the cell", cfg)
        assert verdict.source == "fuzzy-only" and verdict.correct

    def test_malformed_reply_is_treated_as_unavailable(self, cfg, monkeypatch):
        # ask_judge returns None for unparseable replies; grade must not raise.
        monkeypatch.setattr(grade_mod, "ask_judge", lambda *a, **k: None)
        verdict = grade("q", "the powerhouse of the cell", "it makes energy for the cell", cfg)
        assert verdict.source == "fuzzy-only"

    def test_ask_judge_swallows_connection_errors(self, cfg, monkeypatch):
        # ask_judge owns its own error handling. If a URLError ever leaked, the session would
        # die mid-review over a background service being down.
        def boom(*a, **k):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(grade_mod.urllib.request, "urlopen", boom)
        assert grade_mod.ask_judge("q", "a", "t", cfg) is None

    def test_ask_judge_returns_none_for_an_unparseable_reply(self, cfg, monkeypatch):
        monkeypatch.setattr(
            grade_mod.urllib.request, "urlopen", _fake_urlopen('{"response": "maybe?"}')
        )
        assert grade_mod.ask_judge("q", "a", "t", cfg) is None

    @pytest.mark.parametrize(
        "reply,expected", [("CORRECT", True), ("INCORRECT", False), ("  correct  ", True)]
    )
    def test_ask_judge_parses_verdicts(self, cfg, monkeypatch, reply, expected):
        monkeypatch.setattr(
            grade_mod.urllib.request,
            "urlopen",
            _fake_urlopen(json.dumps({"response": reply})),
        )
        assert grade_mod.ask_judge("q", "a", "t", cfg) is expected


class TestThresholdConfig:
    def test_thresholds_are_configurable(self, monkeypatch):
        monkeypatch.setenv("AVR_FUZZY_CORRECT", "0.99")
        monkeypatch.setenv("AVR_FUZZY_WRONG", "0.98")
        cfg = Config()
        calls = stub_judge(monkeypatch, True)
        # "Paris" vs "Parris" is well below 0.98 now, so it should short-circuit to incorrect.
        verdict = grade("q", "Paris", "Parris", cfg)
        assert not verdict.correct
        assert calls == []

    def test_invalid_thresholds_are_reported(self, monkeypatch):
        monkeypatch.setenv("AVR_FUZZY_CORRECT", "0.2")
        monkeypatch.setenv("AVR_FUZZY_WRONG", "0.8")
        assert Config().validate(), "inverted thresholds must be flagged"


class TestMathNotation:
    """Spoken maths transcribes as words; cards write it as symbols. Without expansion a
    correct answer to a maths cloze scores 0.22 — below the clearly-wrong floor, so it is
    marked wrong and never even reaches the judge."""

    def test_power_notation_matches_spoken_form(self, cfg):
        assert fuzzy_score("2^K", "2 to the power of K") >= 0.95

    def test_power_notation_with_terminator_stripped(self, cfg):
        assert fuzzy_score("2^K", "2 to the power of K done.") >= 0.95

    def test_equals_is_pronounced(self, cfg):
        assert fuzzy_score("L = 2^K", "L equals 2 to the power of K") >= 0.95

    def test_hyphenated_words_are_not_turned_into_minus(self, cfg):
        # "-" is deliberately not expanded: "K-bit" must not become "k minus bit".
        assert fuzzy_score("K-bit", "K-bit") == 1.0

    def test_latex_commands_are_spoken(self, cfg):
        assert fuzzy_score(r"a \times b", "a times b") >= 0.95


class TestShortAnswerStrictness:
    def test_wrong_variable_in_a_short_answer_is_not_auto_correct(self, cfg, monkeypatch):
        # 0.947 on a long expanded string, but naming the wrong variable is the whole answer
        # being wrong. It must reach the judge rather than sail through as correct.
        calls = stub_judge(monkeypatch, False)
        verdict = grade("q", "2^K", "2 to the power of N", cfg)
        assert not verdict.correct
        assert verdict.source == "judge"
        assert len(calls) == 1

    def test_the_right_variable_still_passes_without_the_judge(self, cfg, monkeypatch):
        calls = stub_judge(monkeypatch, False)
        verdict = grade("q", "2^K", "2 to the power of K", cfg)
        assert verdict.correct
        assert verdict.source == "fuzzy"
        assert calls == []

    def test_a_long_answer_keeps_the_normal_threshold(self, cfg):
        answer = "the sinoatrial node located in the right atrium"
        assert correct_threshold(answer, cfg) == cfg.fuzzy_correct

    def test_a_short_answer_gets_the_strict_threshold(self, cfg):
        assert correct_threshold("2^K", cfg) == SHORT_ANSWER_CORRECT

    def test_containment_still_rescues_a_short_answer_in_a_sentence(self, cfg, monkeypatch):
        calls = stub_judge(monkeypatch, False)
        verdict = grade("q", "Paris", "The capital of France is Paris.", cfg)
        assert verdict.correct and calls == []
