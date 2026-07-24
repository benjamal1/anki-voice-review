"""Session state machine — scripted transcripts, no mic, speakers, Anki, or Ollama."""

import pytest

from avr.cards import Card
from avr.config import EASE_AGAIN, EASE_EASY, EASE_GOOD, EASE_HARD, Config
from avr.grade import Verdict
from avr.session import (
    AnswerCard,
    NextCard,
    Phase,
    Quit,
    Session,
    ShowAnswer,
    Speak,
    StartOverrideTimer,
    match_command,
    split_terminator,
)

CARD = Card(
    card_id=1,
    question="What is the capital of France?",
    answer="Paris",
    raw_question_html="",
    raw_answer_html="",
)


def always(correct: bool):
    return lambda q, a, t, cfg: Verdict(correct, 1.0 if correct else 0.0, "stub")


def session(correct=True, **env):
    cfg = Config(**env) if env else Config()
    s = Session(cfg=cfg, grade_fn=always(correct))
    s.begin_card(CARD)
    return s


def of(intents, kind):
    return [i for i in intents if isinstance(i, kind)]


class TestMatchCommand:
    @pytest.mark.parametrize("word", ["again", "hard", "good", "easy", "repeat", "skip", "quit"])
    def test_recognises_whole_utterance_commands(self, word):
        assert match_command(word) == word

    def test_is_case_and_punctuation_insensitive(self):
        assert match_command("Good.") == "good"

    def test_does_not_match_a_command_inside_a_sentence(self):
        # The bug this prevents: a card answered "good cholesterol" would otherwise submit a
        # grade mid-answer and the user would never learn why.
        assert match_command("good cholesterol lowers risk") is None
        assert match_command("the again reflex") is None

    def test_rejects_unknown_words(self):
        assert match_command("banana") is None


class TestSplitTerminator:
    def test_bare_terminator_terminates_with_no_speech(self):
        assert split_terminator("done", "done") == ("", True)

    def test_trailing_terminator_is_stripped_from_the_answer(self):
        # VAD usually packages answer and terminator into one utterance.
        speech, terminated = split_terminator("the capital is Paris done", "done")
        assert terminated
        assert "done" not in speech
        assert "paris" in speech

    def test_terminator_inside_a_sentence_does_not_terminate(self):
        assert split_terminator("I am done with this topic anyway", "done")[1] is False

    def test_plain_speech_does_not_terminate(self):
        assert split_terminator("the capital is Paris", "done") == ("the capital is Paris", False)


class TestAnswerAccumulation:
    def test_speaking_does_not_grade_until_the_terminator(self):
        s = session()
        assert s.on_line("the capital") == []
        assert s.on_line("of France") == []
        assert s.phase is Phase.LISTENING
        assert s.graded == 0

    def test_a_mid_answer_pause_does_not_end_the_turn(self):
        # Multiple VAD segments are one answer, not three.
        s = session()
        s.on_line("um")
        s.on_line("I think it is")
        s.on_line("Paris")
        assert s.graded == 0
        s.on_line("done")
        assert s.graded == 1

    def test_terminator_triggers_grading(self):
        s = session(correct=True)
        s.on_line("Paris")
        intents = s.on_line("done")
        assert of(intents, ShowAnswer)
        assert s.phase is Phase.OVERRIDE

    def test_whole_answer_is_passed_to_the_grader(self):
        seen = {}

        def spy(q, a, t, cfg):
            seen["transcript"] = t
            return Verdict(True, 1.0, "stub")

        s = Session(cfg=Config(), grade_fn=spy)
        s.begin_card(CARD)
        s.on_line("it is")
        s.on_line("Paris done")
        assert "it is" in seen["transcript"] and "paris" in seen["transcript"].lower()

    def test_blank_lines_are_ignored(self):
        s = session()
        assert s.on_line("   ") == []


class TestVerdictAnnouncement:
    def test_correct_speaks_the_verdict_and_starts_the_window(self):
        s = session(correct=True)
        intents = s.on_line("Paris done")
        assert "Correct" in [i.text for i in of(intents, Speak)]
        assert of(intents, StartOverrideTimer)

    def test_incorrect_also_speaks_the_right_answer(self):
        # Hearing the answer is the point of getting one wrong.
        s = session(correct=False)
        spoken = [i.text for i in of(s.on_line("Berlin done"), Speak)]
        assert "Incorrect" in spoken
        assert "Paris" in spoken

    def test_correct_does_not_read_the_answer_back(self):
        s = session(correct=True)
        spoken = [i.text for i in of(s.on_line("Paris done"), Speak)]
        assert "Paris" not in spoken, "no reason to re-read an answer already given correctly"


class TestOverrideWindow:
    def test_default_good_on_correct_when_the_window_expires(self):
        s = session(correct=True)
        s.on_line("Paris done")
        intents = s.on_override_expired()
        assert of(intents, AnswerCard)[0].ease == EASE_GOOD
        assert of(intents, NextCard)

    def test_default_again_on_incorrect_when_the_window_expires(self):
        s = session(correct=False)
        s.on_line("Berlin done")
        assert of(s.on_override_expired(), AnswerCard)[0].ease == EASE_AGAIN

    @pytest.mark.parametrize(
        "word,ease", [("again", EASE_AGAIN), ("hard", EASE_HARD), ("good", EASE_GOOD), ("easy", EASE_EASY)]
    )
    def test_spoken_ease_overrides_the_graded_default(self, word, ease):
        s = session(correct=True)  # would default to Good
        s.on_line("Paris done")
        intents = s.on_line(word)
        assert of(intents, AnswerCard)[0].ease == ease

    def test_override_wins_over_a_later_expiry(self):
        # The runner may still fire the timer after an override landed; that must not
        # double-submit a grade for the same card.
        s = session(correct=True)
        s.on_line("Paris done")
        s.on_line("again")
        assert s.on_override_expired() == []

    def test_stray_talk_during_the_window_is_ignored(self):
        s = session(correct=True)
        s.on_line("Paris done")
        assert s.on_line("hmm okay whatever") == []
        assert s.phase is Phase.OVERRIDE

    def test_quit_works_during_the_window(self):
        s = session(correct=True)
        s.on_line("Paris done")
        assert of(s.on_line("quit"), Quit)


class TestCommands:
    def test_direct_ease_skips_grading_entirely(self):
        s = session(correct=False)
        intents = s.on_line("easy")
        assert of(intents, AnswerCard)[0].ease == EASE_EASY
        assert of(intents, ShowAnswer)
        assert of(intents, NextCard)

    def test_repeat_respeaks_the_question_without_grading(self):
        s = session()
        s.on_line("some partial answer")
        intents = s.on_line("repeat")
        assert [i.text for i in of(intents, Speak)] == [CARD.question]
        assert s.graded == 0

    def test_repeat_discards_the_partial_answer(self):
        # Whatever was captured was against a card the user just said they did not hear.
        s = session()
        s.on_line("wrong guess")
        s.on_line("repeat")
        assert s.buffer == []

    def test_skip_advances_without_grading(self):
        s = session()
        intents = s.on_line("skip")
        assert of(intents, NextCard)
        assert not of(intents, AnswerCard)
        assert s.graded == 0

    def test_quit_ends_the_session(self):
        s = session()
        intents = s.on_line("quit")
        assert of(intents, Quit)
        assert s.phase is Phase.FINISHED

    def test_nothing_is_processed_after_quitting(self):
        s = session()
        s.on_line("quit")
        assert s.on_line("good") == []


class TestTally:
    def test_counts_graded_and_correct(self):
        s = session(correct=True)
        s.on_line("Paris done")
        s.on_override_expired()
        s.begin_card(CARD)
        s.on_line("Paris done")
        assert s.graded == 2 and s.correct == 2

    def test_direct_ease_counts_toward_the_tally(self):
        s = session()
        s.on_line("again")
        assert s.graded == 1 and s.correct == 0


class TestConfigurableTerminator:
    def test_terminator_keyword_is_configurable(self, monkeypatch):
        monkeypatch.setenv("AVR_TERMINATOR", "finished")
        s = Session(cfg=Config(), grade_fn=always(True))
        s.begin_card(CARD)
        assert s.on_line("done") == [] or s.graded == 0
        s.on_line("Paris finished")
        assert s.graded == 1


class TestBareTerminatorMeansIDontKnow:
    """Saying just the end word, with nothing before it, is a deliberate 'I don't know' — the
    fastest hands-free way to mark a card wrong. It must grade, not prompt again."""

    def test_bare_terminator_grades_incorrect(self):
        s = session(correct=True)  # grader would say correct; it must not be consulted
        s.on_line("done")
        assert s.graded == 1
        assert s.last_verdict is not None and not s.last_verdict.correct

    def test_it_does_not_call_the_grader(self):
        called = []

        def spy(q, a, t, cfg):
            called.append(t)
            return Verdict(True, 1.0, "stub")

        s = Session(cfg=Config(), grade_fn=spy)
        s.begin_card(CARD)
        s.on_line("done")
        assert called == [], "there is nothing to grade; the verdict is not a judgement call"

    def test_the_answer_is_still_read_back(self):
        # Hearing the answer is the entire point of not knowing it.
        s = session()
        spoken = [i.text for i in of(s.on_line("done"), Speak)]
        assert "Incorrect" in spoken
        assert CARD.answer in spoken

    def test_it_defaults_to_again_and_can_still_be_overridden(self):
        s = session()
        s.on_line("done")
        assert of(s.on_override_expired(), AnswerCard)[0].ease == EASE_AGAIN

    def test_override_still_wins_after_a_bare_terminator(self):
        s = session()
        s.on_line("done")
        assert of(s.on_line("good"), AnswerCard)[0].ease == EASE_GOOD

    def test_the_verdict_is_labelled_so_it_is_distinguishable(self):
        s = session()
        s.on_line("done")
        assert s.last_verdict.source == "no answer"
