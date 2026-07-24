"""Grade a spoken answer against the card's answer text.

Two stages, cheapest first. Fuzzy matching settles the clear cases with no model call at all;
only the ambiguous middle band reaches the local LLM. That ordering exists for latency — most
cards never pay for a model call.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from difflib import SequenceMatcher

from .cards import normalize
from .config import Config

log = logging.getLogger(__name__)

# A containment window must be at least this similar before it counts as "they said it".
CONTAINMENT_MIN = 0.8

# Short answers need a stricter bar before being auto-marked correct without the judge.
#
# String similarity measures characters, but meaning lives in tokens. On a terse answer one
# wrong token IS the answer, while barely moving the score: "2^K" against a spoken "2 to the
# power of N" scores 0.947 once notation is expanded, comfortably above the normal 0.75, so
# naming the wrong variable would sail through as correct. Demanding near-identity on short
# answers routes those to the judge instead. Long answers are unaffected — there, a single
# differing word genuinely is a small error.
SHORT_ANSWER_TOKENS = 3
SHORT_ANSWER_CORRECT = 0.95

JUDGE_PROMPT = """You are grading a flashcard answer spoken aloud and transcribed by speech recognition.

Question: {question}
Correct answer: {answer}
What the student said: {transcript}

Did the student convey the correct answer? Ignore wording, word order, filler words, and
transcription noise. Judge only whether the meaning matches.

Reply with exactly one word: CORRECT or INCORRECT."""


@dataclass(frozen=True)
class Verdict:
    correct: bool
    score: float
    source: str  # "fuzzy" | "judge" | "fuzzy-fallback"
    detail: str = ""


def _containment_ratio(needle: str, haystack: str) -> float:
    """Best similarity between `needle` and any equal-word-count window of `haystack`.

    Whole-string similarity is the wrong measure for spoken answers. A card answering "Paris"
    against someone saying "the capital is Paris" scores only 0.40 on a plain ratio — length
    mismatch dominates and a correct answer lands near the clearly-wrong floor.

    Windows are measured in **words, not characters**. Character windows let a short answer
    slide across any long sentence until it finds a coincidental match: "Paris" scores 0.40
    against "photosynthesis" purely on shared letters, which is indistinguishable from a real
    hit. Word boundaries make the comparison mean "did they say this", which is the question
    grading actually asks.
    """
    needle_words = needle.split()
    haystack_words = haystack.split()
    span = len(needle_words)
    if not span or len(haystack_words) < span:
        return 0.0

    best = 0.0
    # Answers are a handful of words, so a plain scan beats pulling in a fuzzy-match library.
    for start in range(len(haystack_words) - span + 1):
        window = " ".join(haystack_words[start : start + span])
        best = max(best, SequenceMatcher(None, needle, window).ratio())
        if best == 1.0:
            break
    return best


def fuzzy_score(answer: str, transcript: str) -> float:
    """Similarity of a spoken answer to the card's answer, 0.0-1.0.

    Takes the better of whole-string similarity and best-window containment, so answering in a
    full sentence is not penalised while an answer that simply is not there still scores low.

    Directional on purpose: the card's answer is the needle and the transcript is the haystack.
    Saying the answer plus padding is correct; saying a fragment of a long answer is not.
    """
    a, b = normalize(answer), normalize(transcript)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0

    whole = SequenceMatcher(None, a, b).ratio()
    contained = _containment_ratio(a, b)
    # Containment exists to rescue a near-verbatim answer buried in a sentence, so it only
    # counts when the window is a genuine hit. Below the bar it is coincidence, not signal:
    # "paris" scores 0.57 against the word "process" on shared letters alone, which would
    # otherwise drag unrelated answers up into the judge band and cost latency on every card.
    return max(whole, contained if contained >= CONTAINMENT_MIN else 0.0)


def ask_judge(question: str, answer: str, transcript: str, cfg: Config) -> bool | None:
    """Ask the local model. Returns None when unreachable or unparseable — never raises.

    A dead Ollama must degrade the grade, not end the review session.
    """
    body = json.dumps(
        {
            "model": cfg.ollama_model,
            "prompt": JUDGE_PROMPT.format(
                question=question or "(not available)", answer=answer, transcript=transcript
            ),
            "stream": False,
            # Grading is a classification, not a creative task; temperature 0 also makes the
            # single-word reply constraint far more reliable.
            "options": {"temperature": 0, "num_predict": 8},
        }
    ).encode()

    request = urllib.request.Request(
        f"{cfg.ollama_url}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=cfg.judge_timeout_s) as response:
            payload = json.loads(response.read().decode())
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        log.warning("judge unavailable (%s); falling back to fuzzy", exc)
        return None

    reply = str(payload.get("response", "")).strip().upper()
    if "INCORRECT" in reply:
        return False
    if "CORRECT" in reply:
        return True
    log.warning("judge reply not understood (%r); falling back to fuzzy", reply[:80])
    return None


def correct_threshold(answer: str, cfg: Config) -> float:
    """The similarity a spoken answer must reach to be marked correct with no model call.

    Terse answers get a stricter bar — see SHORT_ANSWER_CORRECT. Counted on the raw answer,
    before notation expansion, because "2^K" is one idea however many words it expands into.
    """
    if len(answer.split()) <= SHORT_ANSWER_TOKENS:
        return max(cfg.fuzzy_correct, SHORT_ANSWER_CORRECT)
    return cfg.fuzzy_correct


def grade(question: str, answer: str, transcript: str, cfg: Config) -> Verdict:
    """Fuzzy first; the judge only sees the ambiguous band."""
    score = fuzzy_score(answer, transcript)
    correct_at = correct_threshold(answer, cfg)

    if score >= correct_at:
        return Verdict(True, score, "fuzzy", "clear match")
    if score < cfg.fuzzy_wrong:
        return Verdict(False, score, "fuzzy", "clearly different")

    if not cfg.use_judge:
        return Verdict(False, score, "fuzzy-only", "judge disabled, ambiguous band")

    judged = ask_judge(question, answer, transcript, cfg)
    if judged is None:
        # Nothing better available. Bias the ambiguous band toward "wrong" so a card you may
        # not know does not get pushed weeks out on a coin flip; Again is the cheap mistake.
        return Verdict(False, score, "fuzzy-fallback", "judge unavailable, ambiguous band")
    return Verdict(judged, score, "judge", "semantic verdict")
