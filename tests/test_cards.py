"""Card extraction — pure functions over rendered HTML, no Anki required."""

from avr.cards import answer_text, is_cloze, normalize, strip_html

# Shapes below mirror what guiCurrentCard actually returns.
BASIC_QUESTION = '<div class="front">What is the capital of France?</div>'
BASIC_ANSWER = (
    '<div class="front">What is the capital of France?</div>\n\n<hr id=answer>\n\n'
    '<div class="back">Paris</div>'
)

CLOZE_QUESTION = 'The mitochondrion is the <span class="cloze">[...]</span> of the cell'
CLOZE_ANSWER = 'The mitochondrion is the <span class="cloze">powerhouse</span> of the cell'

CLOZE_MULTI_ANSWER = (
    'The <span class="cloze">heart</span> pumps '
    '<span class="cloze-inactive">blood</span> through the '
    '<span class="cloze">aorta</span>'
)


class TestStripHtml:
    def test_removes_tags(self):
        assert strip_html("<div><b>Paris</b></div>") == "Paris"

    def test_removes_sound_references(self):
        assert strip_html("Paris [sound:pronounce_paris.mp3]") == "Paris"

    def test_block_tags_become_word_boundaries(self):
        # Without this, adjacent divs collapse into "onetwo" and grading compares nonsense.
        assert strip_html("<div>one</div><div>two</div>") == "one two"

    def test_collapses_whitespace_and_nbsp(self):
        assert strip_html("a  \n  b") == "a b"

    def test_decodes_entities(self):
        assert strip_html("caf&eacute; &amp; bar") == "café & bar"


class TestNormalize:
    def test_folds_case_and_punctuation(self):
        assert normalize("Paris, France!") == "paris france"

    def test_strips_accents(self):
        assert normalize("café") == "cafe"

    def test_is_idempotent(self):
        once = normalize("Hello, World!")
        assert normalize(once) == once


class TestIsCloze:
    def test_detects_cloze_span(self):
        assert is_cloze(CLOZE_ANSWER)

    def test_basic_card_is_not_cloze(self):
        assert not is_cloze(BASIC_ANSWER)

    def test_ignores_inactive_only_cloze_as_still_cloze(self):
        # cloze-inactive contains "cloze" as a token boundary case; a card with only inactive
        # spans is still a cloze card, but the class token differs and must not false-positive
        # on an unrelated class like "unclozed".
        assert not is_cloze('<span class="unclozed">x</span>')


class TestAnswerText:
    def test_basic_returns_back_field_only(self):
        assert answer_text(BASIC_QUESTION, BASIC_ANSWER) == "Paris"

    def test_basic_does_not_include_the_question(self):
        result = answer_text(BASIC_QUESTION, BASIC_ANSWER)
        assert "capital of France" not in result

    def test_cloze_returns_deletion_only(self):
        # The rest of the sentence was already read aloud; grading against it would mark
        # anyone correct who simply parroted the question back.
        assert answer_text(CLOZE_QUESTION, CLOZE_ANSWER) == "powerhouse"

    def test_cloze_joins_multiple_active_deletions(self):
        result = answer_text("", CLOZE_MULTI_ANSWER)
        assert "heart" in result and "aorta" in result
        assert "blood" not in result

    def test_cloze_strips_sound_tags(self):
        html = 'x <span class="cloze">Paris [sound:a.mp3]</span> y'
        assert answer_text("", html) == "Paris"

    def test_falls_back_to_whole_answer_without_separator(self):
        assert answer_text("Q", "<div>Just the answer</div>") == "Just the answer"

    def test_subtracts_question_prefix_when_no_separator(self):
        assert answer_text("<p>Q text</p>", "<p>Q text</p><p>A text</p>") == "A text"

    def test_empty_cloze_falls_back_rather_than_returning_nothing(self):
        # A malformed cloze must not grade every spoken answer as wrong.
        html = 'Some sentence <span class="cloze"></span> here'
        assert answer_text("", html) != ""
