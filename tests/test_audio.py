"""Audio adapters — line cleaning and the echo gate. No mic or speakers required."""

from avr.stt import DUPLICATE_SUPPRESS_S, FakeTranscriber, clean_line, is_duplicate_emission
from avr.tts import FakeSpeaker


class TestCleanLine:
    def test_strips_ansi_redraw_codes(self):
        # whisper-stream redraws its current line in place, so stdout carries control codes.
        assert clean_line("\x1b[2K\rthe capital is Paris") == "the capital is Paris"

    def test_drops_blank_audio_marker(self):
        assert clean_line("[BLANK_AUDIO]") == ""

    def test_drops_bracketed_non_speech(self):
        assert clean_line("(buzzing)") == ""
        assert clean_line("[typing]") == ""

    def test_keeps_real_speech(self):
        assert clean_line("  Paris  ") == "Paris"

    def test_keeps_speech_containing_brackets(self):
        # Only a line that is *entirely* a marker should be dropped.
        assert clean_line("the answer (probably) is Paris") != ""

    def test_empty_stays_empty(self):
        assert clean_line("\n") == ""



class TestFakeTranscriber:
    def test_yields_scripted_lines_then_none(self):
        stt = FakeTranscriber(lines=["Paris", "done"])
        assert stt.get(0.1) == "Paris"
        assert stt.get(0.1) == "done"
        assert stt.get(0.01) is None

    def test_a_streaming_repeat_is_suppressed(self):
        # Simulates whisper_step_ms > 0 re-transcribing the same trailing audio window several
        # times in a row before anything new is said.
        stt = FakeTranscriber(lines=["Good.", "Good.", "Good."])
        assert stt.get(0.1) == "Good."
        assert stt.get(0.01) is None, "the repeats must be dropped, not queued as new lines"
        assert stt.suppressed == 2

    def test_a_repeat_after_the_window_still_comes_through(self):
        stt = FakeTranscriber(lines=["Good.", "Good."], delay=DUPLICATE_SUPPRESS_S + 0.2)
        assert stt.get(0.1) == "Good."
        assert stt.get(0.1) == "Good.", "outside the window, it is a real second utterance"

    def test_a_different_line_is_never_suppressed(self):
        stt = FakeTranscriber(lines=["Good.", "Again."])
        assert stt.get(0.1) == "Good."
        assert stt.get(0.1) == "Again."


class TestDrain:
    """drain() must discard a genuine backlog (content already queued, not yet read) without
    ever being able to erase the not-yet-spoken script — the two are deliberately separate."""

    def test_drain_discards_a_queued_backlog(self):
        stt = FakeTranscriber(lines=[], backlog=["skip", "skip"])
        assert stt.drain() == 2
        assert stt.get(0.01) is None

    def test_drain_never_touches_the_future_script(self):
        # This is the property that keeps every multi-turn worker test correct: a
        # state-transition drain() must not be able to eat a turn the test hasn't reached yet.
        stt = FakeTranscriber(lines=["good", "undo", "again"])
        assert stt.drain() == 0
        assert stt.get(0.01) == "good"
        assert stt.get(0.01) == "undo"

    def test_backlog_is_served_before_the_script(self):
        stt = FakeTranscriber(lines=["later"], backlog=["now"])
        assert stt.get(0.01) == "now"
        assert stt.get(0.01) == "later"


class TestIsDuplicateEmission:
    def test_no_previous_line_is_never_a_duplicate(self):
        assert not is_duplicate_emission("", 0.0, "Good.", 1.0)

    def test_same_text_within_the_window_is_a_duplicate(self):
        assert is_duplicate_emission("Good.", 10.0, "good", 10.5)

    def test_case_and_punctuation_do_not_defeat_suppression(self):
        assert is_duplicate_emission("good", 10.0, "Good!", 10.1)

    def test_same_text_outside_the_window_is_not_a_duplicate(self):
        assert not is_duplicate_emission("Good.", 10.0, "Good.", 10.0 + DUPLICATE_SUPPRESS_S + 1)

    def test_different_text_is_never_a_duplicate(self):
        assert not is_duplicate_emission("Good.", 10.0, "Again.", 10.1)


class TestWhisperControlLines:
    """In VAD mode (--step 0) whisper-stream wraps every utterance in marker lines. Passing
    them through appends 'Transcription 1 START t0 0 ms' to the answer, which wrecks the fuzzy
    score and sends every card to the judge or to a wrong verdict."""

    def test_transcription_start_marker_is_dropped(self):
        assert clean_line("### Transcription 1 START | t0 = 0 ms | t1 = 3000 ms") == ""

    def test_transcription_end_marker_is_dropped(self):
        assert clean_line("### Transcription 1 END") == ""

    def test_start_speaking_banner_is_dropped(self):
        assert clean_line("[Start speaking]") == ""

    def test_whisper_init_chatter_is_dropped(self):
        assert clean_line("whisper_init_with_params_no_state: loading model") == ""
        assert clean_line("main: processing 1 samples") == ""

    def test_the_speech_between_the_markers_survives(self):
        assert clean_line("the capital is Paris") == "the capital is Paris"

    def test_a_sentence_starting_with_hash_is_still_dropped_only_when_a_marker(self):
        # "###" only ever prefixes whisper's own output; real speech is never transcribed
        # with leading hashes.
        assert clean_line("### Transcription 12 START") == ""


class TestSilenceHallucinations:
    """base.en emits these on silence — an artifact of its training data, not speech."""

    def test_bare_you_is_dropped(self):
        assert clean_line("you") == ""

    def test_thank_you_on_its_own_is_dropped(self):
        assert clean_line("Thank you.") == ""

    def test_thanks_for_watching_is_dropped(self):
        assert clean_line("Thanks for watching!") == ""

    def test_the_same_words_inside_a_real_sentence_survive(self):
        # Only whole-line matches are dropped, so a genuine answer containing them is safe.
        assert clean_line("thank you notes are the answer") != ""
        assert clean_line("you press the button") != ""


class TestRealWhisperOutput:
    """These are verbatim lines from a real session's trace log. My earlier tests fed clean
    strings like "skip"; whisper actually prefixes every line with a timestamp span, and that
    prefix broke every whole-utterance command match — the real cause of "skip does nothing"."""

    def test_timestamped_command_cleans_to_the_bare_word(self):
        assert clean_line("[00:00:00.000 --> 00:00:04.000]   Skip.") == "Skip."

    def test_timestamped_answer_cleans_to_the_words(self):
        assert clean_line("[00:00:00.000 --> 00:00:04.240]   Proprioception done.") == "Proprioception done."

    def test_a_cleaned_command_is_recognised(self):
        from avr.session import match_command

        assert match_command(clean_line("[00:00:00.000 --> 00:00:04.000]   Skip.")) == "skip"
        assert match_command(clean_line("[00:00:00.000 --> 00:00:02.000]   again")) == "again"
        assert match_command(clean_line("[00:00:00.000 --> 00:00:02.000]   Good.")) == "good"

    def test_keyboard_clicking_noise_is_dropped(self):
        assert clean_line("[00:00:00.000 --> 00:00:03.000]   (keyboard clicking)") == ""

    def test_bare_done_still_terminates(self):
        from avr.session import split_terminator

        speech, terminated = split_terminator(clean_line("[00:00:00.000 --> 00:00:03.000]   Done."), "done")
        assert terminated and speech == ""
