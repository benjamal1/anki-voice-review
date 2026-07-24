"""Card extraction — pure functions over rendered HTML, no Anki required."""

from avr.cards import (
    Card,
    answer_text,
    cloze_by_diff,
    is_cloze,
    normalize,
    speakable,
    strip_html,
    words_to_numbers,
)

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


# What card.question() actually returns: Anki renders the note type's CSS into the card HTML.
RENDERED_WITH_STYLE = """<style>
.card {
 font-family: arial;
 font-size: 20px;
 text-align: center;
 color: black;
 background-color: white;
}
.cloze { font-weight: bold; color: blue; }
</style>
<div class="front">What is the capital of France?</div>"""

RENDERED_ANSWER_WITH_STYLE = (
    RENDERED_WITH_STYLE + '\n\n<hr id=answer>\n\n<div class="back">Paris</div>'
)


class TestStyleAndMarkupAreNotSpoken:
    """Anki renders the note type's stylesheet into the card, and HTMLParser reports its text
    like any other. Without filtering, the loop reads 'font-size 20px text-align center'
    aloud and grades spoken answers against it."""

    def test_style_block_is_not_read_aloud(self):
        spoken = strip_html(RENDERED_WITH_STYLE)
        assert spoken == "What is the capital of France?"

    def test_no_css_property_names_leak_into_the_text(self):
        spoken = strip_html(RENDERED_WITH_STYLE).lower()
        for token in ("font-size", "font-family", "text-align", "background-color", "arial"):
            assert token not in spoken

    def test_card_question_of_a_styled_card_is_clean(self):
        card = Card.from_html(1, RENDERED_WITH_STYLE, RENDERED_ANSWER_WITH_STYLE)
        assert card.question == "What is the capital of France?"
        assert card.answer == "Paris"

    def test_style_does_not_pollute_a_cloze_answer(self):
        html = (
            "<style>.cloze { color: blue; font-size: 30px; }</style>"
            'The mitochondrion is the <span class="cloze">powerhouse</span> of the cell'
        )
        assert answer_text("", html) == "powerhouse"

    def test_script_content_is_not_read_aloud(self):
        html = "<script>var x = 1; alert('hi');</script><div>Real content</div>"
        assert strip_html(html) == "Real content"

    def test_html_comments_are_not_read_aloud(self):
        assert strip_html("<!-- FSRS debug: difficulty 5.2 --><div>Paris</div>") == "Paris"

    def test_anki_playback_directives_are_stripped(self):
        assert strip_html("[anki:play:q:0]Paris") == "Paris"


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


# Captured verbatim from a real card via `avr peek --raw`. Cloze inside MathJax, plus the
# FSRS Helper add-on's injected status widget.
REAL_STYLE = """<style>.card {
    font-family: arial;
    font-size: 20px;
    text-align: center;
    color: black;
    background-color: white;
}
.cloze {
    font-weight: bold;
    color: blue;
}
.nightMode .cloze {
    color: lightblue;
}
</style>"""

REAL_QUESTION = REAL_STYLE + r"A K-bit image stores \(L = [...]\) gray levels per pixel."
REAL_ANSWER = (
    REAL_STYLE
    + r"A K-bit image stores \(L = 2^K\) gray levels per pixel.<br>"
    + r"\(L = 2^K\); 8 bits gives 256 levels (standard grayscale).<br>"
    + '<span id="FSRS_status" style="font-size:12px;opacity:0.5;font-family:monospace;">\n'
    "        FSRS: enabled\n        <br>D: \n        <br>S: \n        <br>R: \n        </span>"
)


class TestRealCardFromTheCollection:
    """Regression cover for the three things a real card surfaced that synthetic HTML did not."""

    def test_fsrs_widget_is_not_spoken(self):
        card = Card.from_html(1, REAL_QUESTION, REAL_ANSWER)
        assert "FSRS" not in card.answer
        assert "D:" not in card.answer

    def test_css_is_not_spoken(self):
        card = Card.from_html(1, REAL_QUESTION, REAL_ANSWER)
        assert "font-family" not in card.question
        assert card.question.startswith("A K-bit image stores")

    def test_mathjax_delimiters_are_not_spoken(self):
        card = Card.from_html(1, REAL_QUESTION, REAL_ANSWER)
        assert "\\(" not in card.question and "\\)" not in card.question

    def test_mathjax_cloze_grades_against_the_deletion_only(self):
        # Anki emits no span.cloze when the deletion is inside MathJax, so this card looked
        # like a Basic card and graded against the whole sentence plus Back Extra — meaning
        # almost anything said scored correct.
        card = Card.from_html(1, REAL_QUESTION, REAL_ANSWER)
        assert card.answer == "2^K"

    def test_back_extra_is_not_part_of_the_grading_target(self):
        card = Card.from_html(1, REAL_QUESTION, REAL_ANSWER)
        assert "256 levels" not in card.answer
        assert "gray levels per pixel" not in card.answer

    def test_closing_span_of_a_skipped_widget_does_not_swallow_the_card(self):
        # If the skip-depth closed on tag membership rather than the tag actually opened,
        # </span> would never balance and everything after it would vanish.
        html = '<span id="FSRS_status">noise</span><div>Real answer</div>'
        assert strip_html(html) == "Real answer"


class TestClozeByDiff:
    def test_returns_empty_without_a_placeholder(self):
        assert cloze_by_diff("plain question", "plain answer") == ""

    def test_recovers_a_simple_unmarked_deletion(self):
        assert cloze_by_diff("The capital is [...] today", "The capital is Paris today") == "Paris"

    def test_ignores_text_appended_only_on_the_answer_side(self):
        result = cloze_by_diff("Capital is [...]", "Capital is Paris. Extra context here.")
        assert "Extra context" not in result


class TestSpeakable:
    def test_placeholder_becomes_the_word_blank(self):
        # "[...]" read aloud is silence or "bracket dot dot dot"; neither conveys a gap.
        assert speakable("The capital is [...] today") == "The capital is blank today"

    def test_leaves_ordinary_text_alone(self):
        assert speakable("What is the capital of France?") == "What is the capital of France?"

    def test_spoken_question_says_blank_but_grading_is_unaffected(self):
        card = Card.from_html(1, REAL_QUESTION, REAL_ANSWER)
        assert "blank" in card.question
        assert "[...]" not in card.question
        assert card.answer == "2^K", "grading must still align on the raw placeholder"


class TestSpokenNumbers:
    """A card writes "4"; the transcript says "four". Without folding these together a correct
    answer scores near zero, and it looks like bad speech recognition rather than two spellings
    of the same number."""

    def test_a_single_word_number_becomes_a_digit(self):
        assert normalize("four") == "4"

    def test_digits_are_left_alone(self):
        assert normalize("4") == "4"

    def test_a_spoken_number_matches_the_written_one(self):
        assert normalize("four") == normalize("4")

    def test_compound_numbers_fold_into_one(self):
        assert words_to_numbers("twenty three") == "23"
        assert words_to_numbers("one hundred fifty") == "150"

    def test_hyphenated_numbers_work_after_normalising(self):
        assert normalize("twenty-three") == "23"

    def test_large_numbers(self):
        assert words_to_numbers("two thousand") == "2000"

    def test_numbers_inside_a_sentence(self):
        assert words_to_numbers("the answer is four cells") == "the answer is 4 cells"

    def test_ordinary_words_are_untouched(self):
        assert words_to_numbers("the mitochondrion") == "the mitochondrion"

    def test_a_number_word_answer_grades_against_a_digit_card(self):
        from avr.grade import fuzzy_score

        assert fuzzy_score("4", "four") == 1.0
        assert fuzzy_score("256", "two hundred fifty six") == 1.0
