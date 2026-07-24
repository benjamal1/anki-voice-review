"""Session state machine — scripted transcripts, no mic, speakers, Anki, or Ollama."""

import pytest

from avr.cards import Card
from avr.config import EASE_AGAIN, EASE_EASY, EASE_GOOD, EASE_HARD, Config
from avr.grade import Verdict
from avr.session import (
    AnswerCard,
    BuryCard,
    FlagCard,
    RegradeCard,
    UndoCard,
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


def session(correct=True, **overrides):
    s = Session(cfg=Config(**overrides), grade_fn=always(correct))
    s.begin_card(CARD)
    return s


def windowed(correct=True, seconds=2.5):
    """A session with the override window switched on. Off by default now: grading advances
    immediately and "undo" corrects the rare disagreement."""
    return session(correct=correct, override_window_s=seconds)


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
        s = windowed(correct=True)
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
        s = windowed(correct=True)
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
        s = windowed(correct=True)
        s.on_line("Paris done")
        intents = s.on_override_expired()
        assert of(intents, AnswerCard)[0].ease == EASE_GOOD
        assert of(intents, NextCard)

    def test_default_again_on_incorrect_when_the_window_expires(self):
        s = windowed(correct=False)
        s.on_line("Berlin done")
        assert of(s.on_override_expired(), AnswerCard)[0].ease == EASE_AGAIN

    @pytest.mark.parametrize(
        "word,ease", [("again", EASE_AGAIN), ("hard", EASE_HARD), ("good", EASE_GOOD), ("easy", EASE_EASY)]
    )
    def test_spoken_ease_overrides_the_graded_default(self, word, ease):
        s = windowed(correct=True)  # would default to Good
        s.on_line("Paris done")
        intents = s.on_line(word)
        assert of(intents, AnswerCard)[0].ease == ease

    def test_override_wins_over_a_later_expiry(self):
        # The runner may still fire the timer after an override landed; that must not
        # double-submit a grade for the same card.
        s = windowed(correct=True)
        s.on_line("Paris done")
        s.on_line("again")
        assert s.on_override_expired() == []

    def test_stray_talk_during_the_window_is_ignored(self):
        s = windowed(correct=True)
        s.on_line("Paris done")
        assert s.on_line("hmm okay whatever") == []
        assert s.phase is Phase.OVERRIDE

    def test_quit_works_during_the_window(self):
        s = windowed(correct=True)
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
        s = windowed()
        s.on_line("done")
        assert of(s.on_override_expired(), AnswerCard)[0].ease == EASE_AGAIN

    def test_override_still_wins_after_a_bare_terminator(self):
        s = windowed()
        s.on_line("done")
        assert of(s.on_line("good"), AnswerCard)[0].ease == EASE_GOOD

    def test_the_verdict_is_labelled_so_it_is_distinguishable(self):
        s = session()
        s.on_line("done")
        assert s.last_verdict.source == "no answer"


class TestBuryWithoutRevealing:
    """Image cards and anything unreadable need setting aside without the answer ever being
    shown or spoken."""

    def test_bury_and_skip_are_the_same_action(self):
        # Not synonyms that happen to behave alike — literally the same action.
        assert match_command("bury") == match_command("skip") == "skip"

    def test_a_trailing_terminator_does_not_break_a_command(self):
        # Saying "skip done" out of habit must skip, not be graded as the answer "skip".
        assert match_command("skip done", terminator="done") == "skip"

    def test_bury_never_shows_the_answer(self):
        s = session()
        intents = s.on_line("bury")
        assert not of(intents, ShowAnswer)

    def test_bury_never_speaks_the_answer(self):
        s = session()
        spoken = [i.text for i in of(s.on_line("bury"), Speak)]
        assert CARD.answer not in spoken

    def test_bury_sets_the_card_aside_and_advances(self):
        s = session()
        intents = s.on_line("bury")
        assert of(intents, BuryCard) and of(intents, NextCard)

    def test_bury_does_not_grade(self):
        s = session()
        s.on_line("bury")
        assert s.graded == 0

    def test_skip_still_works_and_also_never_reveals(self):
        s = session()
        intents = s.on_line("skip")
        assert of(intents, BuryCard) and not of(intents, ShowAnswer)


class TestManualMode:
    """Manual mode never grades. It reads the answer out and waits for the user."""

    def manual(self):
        s = Session(cfg=Config(grading_mode="manual"), grade_fn=always(True))
        s.begin_card(CARD)
        return s

    def test_it_reads_the_answer_and_waits(self):
        s = self.manual()
        intents = s.on_line("my attempt done")
        assert of(intents, ShowAnswer)
        assert CARD.answer in [i.text for i in of(intents, Speak)]
        assert s.phase is Phase.AWAITING_EASE

    def test_it_announces_no_verdict(self):
        s = self.manual()
        spoken = [i.text for i in of(s.on_line("my attempt done"), Speak)]
        assert "Correct" not in spoken and "Incorrect" not in spoken

    def test_it_starts_no_timer(self):
        # A timer would reintroduce exactly the automatic default this mode exists to avoid.
        s = self.manual()
        assert not of(s.on_line("my attempt done"), StartOverrideTimer)

    def test_nothing_happens_until_the_user_speaks(self):
        s = self.manual()
        s.on_line("my attempt done")
        assert s.on_override_expired() == []
        assert s.graded == 0

    @pytest.mark.parametrize(
        "word,ease", [("again", EASE_AGAIN), ("hard", EASE_HARD), ("good", EASE_GOOD), ("easy", EASE_EASY)]
    )
    def test_the_spoken_ease_is_what_gets_submitted(self, word, ease):
        s = self.manual()
        s.on_line("my attempt done")
        assert of(s.on_line(word), AnswerCard)[0].ease == ease

    def test_the_tally_counts_manual_grades(self):
        s = self.manual()
        s.on_line("done")
        s.on_line("good")
        assert s.graded == 1 and s.correct == 1

    def test_stray_talk_while_waiting_is_ignored(self):
        s = self.manual()
        s.on_line("my attempt done")
        assert s.on_line("hmm let me think about that") == []
        assert s.phase is Phase.AWAITING_EASE

    def test_repeat_rereads_the_answer_not_the_question(self):
        s = self.manual()
        s.on_line("done")
        spoken = [i.text for i in of(s.on_line("repeat"), Speak)]
        assert spoken == [CARD.answer]

    def test_bury_works_while_waiting(self):
        s = self.manual()
        s.on_line("done")
        intents = s.on_line("bury")
        assert of(intents, BuryCard) and s.graded == 0

    def test_quit_works_while_waiting(self):
        s = self.manual()
        s.on_line("done")
        assert of(s.on_line("quit"), Quit)


class TestUnresolvedAsksTheUser:
    def test_an_ungradeable_answer_asks_instead_of_guessing(self):
        def unresolved(q, a, t, cfg):
            return Verdict(False, 0.5, "unresolved", "", needs_human=True)

        s = Session(cfg=Config(), grade_fn=unresolved)
        s.begin_card(CARD)
        intents = s.on_line("something ambiguous done")

        assert s.phase is Phase.AWAITING_EASE
        assert not of(intents, StartOverrideTimer), "no default to fall back to"
        assert s.graded == 0, "nothing was decided, so nothing is tallied yet"
        assert CARD.answer in [i.text for i in of(intents, Speak)]

    def test_the_user_then_decides(self):
        def unresolved(q, a, t, cfg):
            return Verdict(False, 0.5, "unresolved", "", needs_human=True)

        s = Session(cfg=Config(), grade_fn=unresolved)
        s.begin_card(CARD)
        s.on_line("something ambiguous done")
        assert of(s.on_line("good"), AnswerCard)[0].ease == EASE_GOOD
        assert s.graded == 1 and s.correct == 1


class TestFlagOnSkip:
    """Optional: skipping can also flag the card, so bad cards can be found again later.
    Red (1) is what the anki-obsidian pipeline watches for."""

    def flagging(self, flag=1):
        s = Session(cfg=Config(flag_on_skip=flag), grade_fn=always(True))
        s.begin_card(CARD)
        return s

    def test_off_by_default(self):
        s = session()
        assert not of(s.on_line("skip"), FlagCard)

    def test_flags_when_configured(self):
        s = self.flagging(1)
        assert of(s.on_line("skip"), FlagCard)[0].flag == 1

    def test_bury_flags_too(self):
        s = self.flagging(1)
        assert of(s.on_line("bury"), FlagCard)[0].flag == 1

    def test_any_colour_works(self):
        s = self.flagging(4)
        assert of(s.on_line("skip"), FlagCard)[0].flag == 4

    def test_the_flag_is_applied_before_the_card_is_buried(self):
        # Once buried the reviewer has moved on and there is no current card left to flag.
        intents = self.flagging(1).on_line("skip")
        kinds = [type(i).__name__ for i in intents]
        assert kinds.index("FlagCard") < kinds.index("BuryCard")

    def test_it_says_which_colour(self):
        spoken = [i.text for i in of(self.flagging(1).on_line("skip"), Speak)]
        assert any("red" in t.lower() for t in spoken)

    def test_flagging_still_never_reveals_the_answer(self):
        intents = self.flagging(1).on_line("skip")
        assert not of(intents, ShowAnswer)
        assert CARD.answer not in [i.text for i in of(intents, Speak)]

    def test_it_still_does_not_grade(self):
        s = self.flagging(1)
        s.on_line("skip")
        assert s.graded == 0

    def test_flagging_works_while_awaiting_a_manual_grade(self):
        s = Session(cfg=Config(grading_mode="manual", flag_on_skip=2), grade_fn=always(True))
        s.begin_card(CARD)
        s.on_line("done")
        assert of(s.on_line("skip"), FlagCard)[0].flag == 2

    def test_an_out_of_range_flag_is_reported(self):
        assert Config(flag_on_skip=9).validate()


class TestImmediateAdvance:
    """Grading advances straight to the next card by default. Making every card wait out a
    window that is usually unused costs more than the rare disagreement, which undo fixes."""

    def test_grading_submits_and_advances_with_no_pause(self):
        s = session(correct=True)
        intents = s.on_line("Paris done")
        assert of(intents, AnswerCard)[0].ease == EASE_GOOD
        assert of(intents, NextCard)
        assert not of(intents, StartOverrideTimer)
        assert s.phase is Phase.LISTENING

    def test_an_incorrect_answer_also_advances_immediately(self):
        s = session(correct=False)
        intents = s.on_line("Berlin done")
        assert of(intents, AnswerCard)[0].ease == EASE_AGAIN
        assert of(intents, NextCard)

    def test_the_answer_is_still_read_back_when_wrong(self):
        s = session(correct=False)
        assert CARD.answer in [i.text for i in of(s.on_line("Berlin done"), Speak)]

    def test_a_window_can_still_be_configured(self):
        s = windowed(correct=True, seconds=2.0)
        intents = s.on_line("Paris done")
        assert of(intents, StartOverrideTimer)[0].seconds == 2.0
        assert not of(intents, AnswerCard), "nothing is submitted until the window closes"


class TestUndo:
    def test_undo_after_a_grade_emits_an_undo_intent(self):
        s = session(correct=True)
        s.on_line("Paris done")
        assert of(s.on_line("undo"), UndoCard)

    def test_undo_rolls_back_the_tally(self):
        s = session(correct=True)
        s.on_line("Paris done")
        assert (s.graded, s.correct) == (1, 1)
        s.on_line("undo")
        assert (s.graded, s.correct) == (0, 0)

    def test_undo_with_nothing_graded_says_so_and_does_nothing(self):
        s = session()
        intents = s.on_line("undo")
        assert not of(intents, UndoCard)
        assert "Nothing to undo" in [i.text for i in of(intents, Speak)]

    def test_undo_works_while_awaiting_a_manual_grade(self):
        s = Session(cfg=Config(grading_mode="manual"), grade_fn=always(True))
        s.begin_card(CARD)
        s.on_line("done")
        s.on_line("good")
        s.on_line("done")
        assert of(s.on_line("undo"), UndoCard)

    def test_go_back_is_the_same_as_undo(self):
        assert match_command("go back") == match_command("undo") == "undo"


class TestConfigurableCommandWords:
    def test_defaults_include_natural_synonyms(self):
        assert match_command("yes") == "good"
        assert match_command("no") == "again"
        assert match_command("pass") == "skip"

    def test_words_can_be_replaced(self):
        cfg = Config(command_words={**Config().command_words, "skip": ["next"]})
        s = Session(cfg=cfg, grade_fn=always(True))
        s.begin_card(CARD)
        assert of(s.on_line("next"), BuryCard)

    def test_replacing_one_action_leaves_the_others_alone(self):
        cfg = Config(command_words={**Config().command_words, "skip": ["next"]})
        assert match_command("good", cfg.command_words) == "good"

    def test_an_empty_word_list_is_reported(self):
        assert Config(command_words={**Config().command_words, "skip": []}).validate()


class TestUndoWaitsInsteadOfRereading:
    """After an undo you have just heard the card and disagreed with the verdict. Reading the
    question again is noise; the only thing left is to say how it should have been graded."""

    def graded_once(self):
        s = session(correct=True)
        s.on_line("Paris done")
        return s

    def test_undo_does_not_reread_the_question(self):
        s = self.graded_once()
        spoken = [i.text for i in of(s.on_line("undo"), Speak)]
        assert CARD.question not in spoken

    def test_undo_waits_for_an_ease(self):
        s = self.graded_once()
        s.on_line("undo")
        assert s.phase is Phase.AWAITING_EASE

    def test_it_acknowledges_briefly(self):
        s = self.graded_once()
        spoken = [i.text for i in of(s.on_line("undo"), Speak)]
        assert spoken == ["Undone"]

    def test_the_acknowledgement_is_not_the_terminator_word(self):
        # Saying the terminator into an open mic invites being transcribed back as a command.
        s = self.graded_once()
        spoken = [i.text.lower() for i in of(s.on_line("undo"), Speak)]
        assert Config().terminator.lower() not in spoken

    def test_the_next_thing_said_regrades_the_card_by_name(self):
        # Undo reverts the scheduling but leaves the reviewer on the card it moved to, so the
        # correction has to name the original card rather than grading what is on screen.
        s = self.graded_once()
        s.on_line("undo")
        intents = s.on_line("again")
        regrade = of(intents, RegradeCard)[0]
        assert regrade.ease == EASE_AGAIN
        assert regrade.card_id == CARD.card_id

    def test_regrading_does_not_advance(self):
        # The card in front of the user has not been answered yet.
        s = self.graded_once()
        s.on_line("undo")
        intents = s.on_line("again")
        assert not of(intents, NextCard)
        assert not of(intents, AnswerCard), "must not grade whatever is on screen"

    def test_grading_after_undo_counts_once(self):
        s = self.graded_once()
        s.on_line("undo")
        s.on_line("again")
        assert s.graded == 1 and s.correct == 0

    def test_resume_card_does_not_speak_or_change_phase(self):
        s = self.graded_once()
        s.on_line("undo")
        assert s.resume_card(CARD) == []
        assert s.phase is Phase.AWAITING_EASE


class TestCommandsWorkInEveryPhase:
    """A command that is silently ignored in one phase is indistinguishable from a broken
    microphone. Every phase that can hear a command must act on it."""

    def phases(self):
        """One session per phase, each parked in that phase."""
        listening = session()

        override = windowed(correct=True)
        override.on_line("Paris done")
        assert override.phase is Phase.OVERRIDE

        awaiting = Session(cfg=Config(grading_mode="manual"), grade_fn=always(True))
        awaiting.begin_card(CARD)
        awaiting.on_line("attempt done")
        assert awaiting.phase is Phase.AWAITING_EASE

        return {"LISTENING": listening, "OVERRIDE": override, "AWAITING_EASE": awaiting}

    def test_skip_works_in_every_phase(self):
        for name, s in self.phases().items():
            assert of(s.on_line("skip"), BuryCard), f"skip did nothing in {name}"

    def test_bury_works_in_every_phase(self):
        for name, s in self.phases().items():
            assert of(s.on_line("bury"), BuryCard), f"bury did nothing in {name}"

    def test_quit_works_in_every_phase(self):
        for name, s in self.phases().items():
            assert of(s.on_line("quit"), Quit), f"quit did nothing in {name}"

    def test_an_ease_works_in_every_phase(self):
        for name, s in self.phases().items():
            intents = s.on_line("again")
            graded = of(intents, AnswerCard) or of(intents, RegradeCard)
            assert graded, f"ease did nothing in {name}"

    def test_skip_during_the_override_window_does_not_grade(self):
        s = windowed(correct=True)
        s.on_line("Paris done")
        intents = s.on_line("skip")
        assert of(intents, BuryCard) and not of(intents, AnswerCard)


class TestManualModeQuickGrading:
    """Manual mode must accept a grade said quickly, however whisper packages it — not only as
    a separate utterance after 'done'."""

    def manual(self):
        s = Session(cfg=Config(grading_mode="manual"), grade_fn=always(True))
        s.begin_card(CARD)
        return s

    def test_a_bare_ease_in_front_grades_immediately(self):
        s = self.manual()
        intents = s.on_line("good")
        assert of(intents, AnswerCard)[0].ease == EASE_GOOD
        assert of(intents, ShowAnswer) and of(intents, NextCard)

    def test_a_compound_done_plus_grade_on_one_line(self):
        # "done good" said in one breath — the whole point of the request.
        s = self.manual()
        intents = s.on_line("done good")
        assert of(intents, AnswerCard)[0].ease == EASE_GOOD

    def test_answer_then_grade_on_one_line(self):
        s = self.manual()
        intents = s.on_line("proprioception again")
        assert of(intents, AnswerCard)[0].ease == EASE_AGAIN

    def test_a_synonym_ease_works_the_same(self):
        s = self.manual()
        assert of(s.on_line("that was correct"), AnswerCard)[0].ease == EASE_GOOD
        s2 = self.manual()
        assert of(s2.on_line("no"), AnswerCard)[0].ease == EASE_AGAIN

    def test_grade_after_done_still_works_two_lines(self):
        # The old two-utterance flow must keep working.
        s = self.manual()
        s.on_line("done")
        assert s.phase is Phase.AWAITING_EASE
        assert of(s.on_line("good"), AnswerCard)[0].ease == EASE_GOOD

    def test_trailing_ease_accepted_in_the_awaiting_phase_too(self):
        s = self.manual()
        s.on_line("done")
        assert of(s.on_line("okay good"), AnswerCard)[0].ease == EASE_GOOD

    def test_done_alone_still_just_flips_and_waits(self):
        s = self.manual()
        intents = s.on_line("done")
        assert not of(intents, AnswerCard), "bare done must not grade"
        assert s.phase is Phase.AWAITING_EASE

    def test_an_answer_without_a_grade_word_does_not_grade(self):
        s = self.manual()
        assert s.on_line("proprioception") == []
        assert s.graded == 0


class TestAutoModeUnaffectedByTrailingEase:
    """The trailing-ease shortcut is manual-only. In auto mode the answer content is graded, so
    an answer ending in a command word must NOT be hijacked into a manual grade."""

    def test_an_auto_answer_ending_in_good_is_still_graded_normally(self):
        seen = {}

        def spy(q, a, t, cfg):
            seen["t"] = t
            return Verdict(True, 1.0, "stub")

        s = Session(cfg=Config(), grade_fn=spy)  # auto
        s.begin_card(CARD)
        s.on_line("cholesterol can be good done")
        assert "good" in seen.get("t", ""), "the whole answer, including 'good', must be graded"
