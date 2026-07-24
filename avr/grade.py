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
    source: str  # "fuzzy" | "judge" | "no answer" | "unresolved"
    detail: str = ""
    # True when nothing could decide this and the person should be asked. There is no verdict
    # to trust here; `correct` is meaningless and must not be acted on.
    needs_human: bool = False


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
            # Without this Ollama unloads the model after a few idle minutes and the next
            # judged card pays seconds to reload it.
            "keep_alive": cfg.ollama_keep_alive,
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


def prewarm(cfg: Config) -> bool:
    """Load the judge model before the session starts.

    A cold model costs several seconds on the first card that needs it, which lands right when
    the user is least expecting a pause. Doing it up front, while they are still settling in,
    makes that cost invisible.
    """
    if cfg.manual:
        return False
    body = json.dumps(
        {"model": cfg.ollama_model, "prompt": "hi", "stream": False,
         "options": {"num_predict": 1}, "keep_alive": cfg.ollama_keep_alive}
    ).encode()
    request = urllib.request.Request(
        f"{cfg.ollama_url}/api/generate", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=cfg.judge_timeout_s):
            return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def grade(question: str, answer: str, transcript: str, cfg: Config) -> Verdict:
    """The model judges every answer. No fuzzy string matching.

    Fuzzy was removed: spoken answers vary too much for a character-similarity threshold, and
    every threshold was a guess that mis-graded reworded-but-correct answers. The local model
    judges meaning directly, which is the only thing that actually works for speech.

    The one non-model shortcut kept is exact equality after normalisation — if what you said
    IS the answer, that is not a judgement call and does not need the model, so it returns
    instantly. This is equality, not similarity: it either matches exactly or goes to the model.
    """
    if cfg.manual:
        # The user grades every card themselves, so any automatic verdict is wasted latency.
        return Verdict(False, 0.0, "manual", "user grades this", needs_human=True)

    if normalize(answer) and normalize(transcript) == normalize(answer):
        return Verdict(True, 1.0, "exact", "said the answer verbatim")

    judged = ask_judge(question, answer, transcript, cfg)
    if judged is None:
        # The model is the only grader now, so if it cannot be reached there is nothing to fall
        # back to. Hand the card to the user rather than invent a verdict.
        return Verdict(
            False, 0.0, "unresolved", "grading model unavailable", needs_human=True
        )
    return Verdict(judged, 1.0 if judged else 0.0, "judge", "semantic verdict")
