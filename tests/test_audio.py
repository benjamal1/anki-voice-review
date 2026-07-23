"""Audio adapters — line cleaning and the echo gate. No mic or speakers required."""

from avr.stt import FakeTranscriber, clean_line
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


class TestEchoGate:
    def test_speaking_drains_the_transcript_backlog(self):
        # The core defence: whisper buffers audio, so lines produced from the TTS can arrive
        # after `say` has already exited. Draining after speaking discards that backlog.
        stt = FakeTranscriber(lines=[])
        speaker = FakeSpeaker()
        speaker.speak("this is the computer talking", gate=stt)
        assert stt.drained == 1

    def test_empty_speech_does_not_touch_the_gate(self):
        stt = FakeTranscriber(lines=[])
        FakeSpeaker().speak("", gate=stt)
        assert stt.drained == 0

    def test_speaking_without_a_gate_is_allowed(self):
        speaker = FakeSpeaker()
        speaker.speak("no gate here")
        assert speaker.said == ["no gate here"]


class TestFakeTranscriber:
    def test_yields_scripted_lines_then_none(self):
        stt = FakeTranscriber(lines=["Paris", "done"])
        assert stt.get(0.1) == "Paris"
        assert stt.get(0.1) == "done"
        assert stt.get(0.01) is None
