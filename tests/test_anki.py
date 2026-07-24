"""AnkiConnect client — the three preflight outcomes, with the HTTP call stubbed."""

import io
import json
import urllib.error

import pytest

from avr import anki as anki_mod
from avr.anki import AnkiConnect, AnkiError, AnkiNotRunning, NoCardShowing
from avr.cards import Card

BASIC_PAYLOAD = {
    "cardId": 1234,
    "question": '<div class="front">Capital of France?</div>',
    "answer": '<div class="front">Capital of France?</div><hr id=answer><div>Paris</div>',
}


def _stub_response(payload: dict):
    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return lambda *a, **k: _Response(json.dumps(payload).encode())


@pytest.fixture
def client():
    return AnkiConnect("http://127.0.0.1:8765")


class TestPreflightOutcomes:
    def test_port_dead_reports_anki_not_running(self, client, monkeypatch):
        def refused(*a, **k):
            raise urllib.error.URLError("Connection refused")

        monkeypatch.setattr(anki_mod.urllib.request, "urlopen", refused)
        with pytest.raises(AnkiNotRunning) as exc:
            client.preflight()
        assert "Open Anki" in str(exc.value)

    def test_idle_reviewer_reports_no_card_showing(self, client, monkeypatch):
        # AnkiConnect signals an idle reviewer with an error string, not a null result. This
        # is the case that actually happens when you forget to click Study Now.
        monkeypatch.setattr(
            anki_mod.urllib.request,
            "urlopen",
            _stub_response({"result": None, "error": "Gui review is not currently active."}),
        )
        with pytest.raises(NoCardShowing) as exc:
            client.current_card()
        assert "Study Now" in str(exc.value)

    def test_null_result_also_reports_no_card_showing(self, client, monkeypatch):
        monkeypatch.setattr(
            anki_mod.urllib.request, "urlopen", _stub_response({"result": None, "error": None})
        )
        with pytest.raises(NoCardShowing):
            client.current_card()

    def test_other_errors_are_not_disguised_as_an_idle_reviewer(self, client, monkeypatch):
        monkeypatch.setattr(
            anki_mod.urllib.request,
            "urlopen",
            _stub_response({"result": None, "error": "collection is not available"}),
        )
        with pytest.raises(AnkiError) as exc:
            client.current_card()
        assert not isinstance(exc.value, NoCardShowing)

    def test_card_showing_returns_the_card(self, client, monkeypatch):
        monkeypatch.setattr(
            anki_mod.urllib.request, "urlopen", _stub_response({"result": BASIC_PAYLOAD})
        )
        card = client.current_card()
        assert card.card_id == 1234
        assert card.answer == "Paris"
        assert "Capital of France" in card.question


class TestCardMapping:
    def test_extracts_spoken_and_graded_text(self):
        card = Card.from_gui_current_card(BASIC_PAYLOAD)
        assert card.question == "Capital of France?"
        assert card.answer == "Paris"

    def test_tolerates_missing_fields(self):
        card = Card.from_gui_current_card({})
        assert card.card_id == 0 and card.question == ""


class TestAnswerCard:
    @pytest.mark.parametrize("ease", [0, 5, -1])
    def test_rejects_out_of_range_ease(self, client, ease):
        # Anki's buttons are 1-4; anything else would be a silent scheduling corruption.
        with pytest.raises(ValueError):
            client.answer_card(ease)
