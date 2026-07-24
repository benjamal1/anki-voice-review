"""Audio adapters — line cleaning and the echo gate. No mic or speakers required."""

from avr.stt import FakeTranscriber, clean_line
from avr.tts import FakeSpeaker, is_echo


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


class TestEchoDetection:
    """Echo is identified by content, not by timing. Discarding everything that arrived
    shortly after speaking also threw away the user answering promptly — which is the single
    most common thing they do, and why spoken commands appeared to do nothing."""

    def test_the_spoken_text_coming_back_is_echo(self):
        spoken = "The mitochondrion is the powerhouse of the cell"
        assert is_echo("the mitochondrion is the powerhouse of the cell", spoken)

    def test_a_mangled_version_of_the_spoken_text_is_still_echo(self):
        spoken = "What is the capital of France?"
        assert is_echo("what is the capital of france", spoken)

    def test_a_command_said_straight_after_speaking_is_not_echo(self):
        assert not is_echo("skip", "What is the capital of France?")

    def test_an_ease_said_straight_after_the_prompt_is_not_echo(self):
        # This is what broke manual mode: the prompt "Good or again?" contains the words it is
        # asking for, so the reply matched it as echo and was discarded.
        assert not is_echo("again", "Good or again?")

    def test_a_recognised_command_is_never_echo(self):
        for word in ("skip", "undo", "good", "again", "quit"):
            assert not is_echo(word, "some card text that was just read aloud")

    def test_a_real_answer_is_not_echo(self):
        assert not is_echo("the powerhouse of the cell", "What does the mitochondrion do?")

    def test_nothing_spoken_means_nothing_is_echo(self):
        assert not is_echo("skip", "")

    def test_the_prompt_no_longer_contains_the_words_it_asks_for(self):
        from avr.cards import Card
        from avr.config import Config
        from avr.session import Session, Speak
        from avr.grade import Verdict

        s = Session(cfg=Config(grading_mode="manual"), grade_fn=lambda *a: Verdict(True, 1.0, "x"))
        s.begin_card(Card(1, "q", "a"))
        spoken = " ".join(
            i.text for i in s.on_line("attempt done") if isinstance(i, Speak)
        ).lower()
        for word in ("good", "again", "hard", "easy"):
            assert word not in spoken

    def test_the_speaker_records_what_it_said(self):
        speaker = FakeSpeaker()
        speaker.speak("hello there")
        assert speaker.last_spoken == "hello there"


class TestFakeTranscriber:
    def test_yields_scripted_lines_then_none(self):
        stt = FakeTranscriber(lines=["Paris", "done"])
        assert stt.get(0.1) == "Paris"
        assert stt.get(0.1) == "done"
        assert stt.get(0.01) is None


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
