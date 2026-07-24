"""Grader — fuzzy scoring and band routing, with the LLM call stubbed."""

import io
import json
import urllib.error

import pytest

from avr import grade as grade_mod
from avr.config import Config, migrate_config
from avr.grade import fuzzy_score, grade


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


class TestAlwaysJudge:
    """Grading now sends every answer to the model. The only non-model path is exact equality."""

    def test_a_verbatim_answer_is_correct_without_the_model(self, cfg, monkeypatch):
        calls = stub_judge(monkeypatch, False)
        verdict = grade("Capital of France?", "Paris", "Paris", cfg)
        assert verdict.correct and verdict.source == "exact"
        assert calls == [], "saying the answer exactly needs no model call"

    def test_verbatim_ignores_case_and_punctuation(self, cfg, monkeypatch):
        stub_judge(monkeypatch, False)
        assert grade("q", "Paris", "paris.", cfg).source == "exact"

    def test_a_spoken_number_matches_a_digit_answer_verbatim(self, cfg, monkeypatch):
        stub_judge(monkeypatch, False)
        assert grade("q", "4", "four", cfg).source == "exact"

    def test_everything_else_goes_to_the_model(self, cfg, monkeypatch):
        calls = stub_judge(monkeypatch, True)
        verdict = grade("q", "the powerhouse of the cell", "it makes energy for the cell", cfg)
        assert verdict.source == "judge" and verdict.correct
        assert len(calls) == 1

    def test_a_reworded_answer_is_judged_correct(self, cfg, monkeypatch):
        stub_judge(monkeypatch, True)
        assert grade("q", "Paris", "the capital is Paris", cfg).correct

    def test_the_model_can_mark_it_wrong(self, cfg, monkeypatch):
        stub_judge(monkeypatch, False)
        assert not grade("q", "Paris", "London", cfg).correct

    def test_manual_mode_never_calls_the_model(self, monkeypatch):
        calls = stub_judge(monkeypatch, True)
        grade("q", "Paris", "London", Config(grading_mode="manual"))
        assert calls == []


class TestModelUnavailable:
    """With no fuzzy fallback, an unreachable model hands the card to the user rather than
    guessing a verdict."""

    def test_model_down_needs_a_human(self, cfg, no_judge):
        verdict = grade("q", "Paris", "the capital is Paris", cfg)
        assert verdict.needs_human and verdict.source == "unresolved"

    def test_a_verbatim_answer_still_works_with_the_model_down(self, cfg, no_judge):
        # Exact equality is not a judgement call, so it does not need the model.
        assert grade("q", "Paris", "Paris", cfg).correct

    def test_a_judged_answer_never_needs_a_human(self, cfg, monkeypatch):
        stub_judge(monkeypatch, True)
        assert not grade("q", "Paris", "the capital is Paris", cfg).needs_human


class TestJudgeErrorHandling:
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



class TestConfigMigration:
    """Anki keeps a user's add-on config across updates, so a new default never reaches anyone
    who has opened the settings dialog — their stored copy of the OLD default wins silently."""

    def stale(self):
        # Captured verbatim from a real installation running the current build.
        return {
            "terminator": "done",
            "override_window_seconds": 2.5,
            "headphones": False,
            "grading_mode": "auto",
            "flag_on_skip": 1,
            "fuzzy_correct": 0.75,
            "fuzzy_wrong": 0.4,
            "say_rate": "250",
            "use_llm_judge": True,
        }

    def test_old_defaults_are_brought_forward(self):
        migrated, changes = migrate_config(self.stale())
        assert migrated["override_window_seconds"] == 0.0
        assert migrated["fuzzy_correct"] == 0.62
        assert migrated["fuzzy_wrong"] == 0.3
        assert changes

    def test_settings_the_user_actually_chose_are_left_alone(self):
        migrated, _ = migrate_config(self.stale())
        assert migrated["say_rate"] == "250", "a deliberate choice must survive"
        assert migrated["flag_on_skip"] == 1

    def test_a_deliberately_different_value_is_not_reset(self):
        stale = self.stale()
        stale["override_window_seconds"] = 4.0  # not the old default, so it was chosen
        migrated, _ = migrate_config(stale)
        assert migrated["override_window_seconds"] == 4.0

    def test_dead_keys_are_removed(self):
        migrated, _ = migrate_config(self.stale())
        assert "use_llm_judge" not in migrated

    def test_turning_the_llm_off_becomes_manual_grading(self):
        stale = self.stale()
        stale["use_llm_judge"] = False
        del stale["grading_mode"]
        migrated, _ = migrate_config(stale)
        assert migrated["grading_mode"] == "manual"

    def test_headphones_default_is_brought_forward(self):
        migrated, changes = migrate_config(self.stale())
        assert migrated["headphones"] is True
        assert changes

    def test_a_deliberate_speakers_choice_would_survive_a_future_default_flip(self):
        # If someone had actually turned headphones ON, that stays; the migration only moves a
        # value that still equals the OLD default.
        stale = self.stale()
        stale["headphones"] = True
        migrated, _ = migrate_config(stale)
        assert migrated["headphones"] is True

    def test_migrating_twice_changes_nothing_the_second_time(self):
        once, _ = migrate_config(self.stale())
        twice, changes = migrate_config(once)
        assert changes == [] and twice == once

    def test_the_migrated_config_produces_the_new_defaults(self):
        migrated, _ = migrate_config(self.stale())
        cfg = Config.from_mapping(migrated)
        assert cfg.override_window_s == 0.0
        assert cfg.fuzzy_correct == 0.62
