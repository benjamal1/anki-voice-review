"""Runtime configuration. Defaults in code, every knob overridable by environment variable.

Every default here targets the Mac, because that is the only machine this runs on: Anki,
AnkiConnect, the microphone, whisper.cpp, `say`, and Ollama all live there.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Anki's ease buttons. Named because `answer_card(card, 3)` at a call site says nothing.
EASE_AGAIN = 1
EASE_HARD = 2
EASE_GOOD = 3
EASE_EASY = 4

# Anki's card flags. 0 means no flag; a card can carry only one.
FLAG_NONE = 0
FLAG_BY_NAME = {
    "none": 0,
    "red": 1,
    "orange": 2,
    "green": 3,
    "blue": 4,
    "pink": 5,
    "turquoise": 6,
    "purple": 7,
}
FLAG_NAMES = {value: name for name, value in FLAG_BY_NAME.items()}

# What you can say, and the words that trigger it. Every action accepts several words because
# speech recognition is not reliable enough to hang a feature on one token, and because people
# reach for different words. All of it is overridable in the add-on config.
DEFAULT_COMMAND_WORDS = {
    "again": ["again", "wrong", "no"],
    "hard": ["hard"],
    "good": ["good", "correct", "yes", "right"],
    "easy": ["easy"],
    "repeat": ["repeat", "again please", "say again"],
    # skip and bury are the same action: set the card aside without grading or revealing it.
    "skip": ["skip", "bury", "pass"],
    "undo": ["undo", "go back", "back"],
    "quit": ["quit", "exit", "finish"],
}

EASE_BY_NAME = {
    "again": EASE_AGAIN,
    "hard": EASE_HARD,
    "good": EASE_GOOD,
    "easy": EASE_EASY,
}


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number, got {raw!r}")


def _merge_command_words(supplied) -> dict:
    """User-supplied words override the defaults per action; unlisted actions keep theirs."""
    words = {action: list(spoken) for action, spoken in DEFAULT_COMMAND_WORDS.items()}
    if isinstance(supplied, dict):
        for action, spoken in supplied.items():
            if action not in words:
                continue
            if isinstance(spoken, str):
                spoken = [part.strip() for part in spoken.split(",")]
            if isinstance(spoken, list):
                cleaned = [str(word).strip() for word in spoken if str(word).strip()]
                if cleaned:
                    words[action] = cleaned
    return words


@dataclass(frozen=True)
class Config:
    # --- Anki ---
    anki_url: str = field(default_factory=lambda: _env_str("AVR_ANKI_URL", "http://127.0.0.1:8765"))

    # --- STT ---
    whisper_bin: str = field(default_factory=lambda: _env_str("AVR_WHISPER_BIN", "whisper-stream"))
    whisper_model: Path = field(
        default_factory=lambda: Path(
            _env_str("AVR_WHISPER_MODEL", "~/whisper-models/ggml-base.en.bin")
        ).expanduser()
    )

    # --- TTS ---
    say_voice: str = field(default_factory=lambda: _env_str("AVR_SAY_VOICE", ""))
    say_rate: str = field(default_factory=lambda: _env_str("AVR_SAY_RATE", "190"))
    # The mic keeps hearing while `say` plays. Audio takes a moment to drain after the process
    # exits, so the gate stays shut a little longer than the call itself.
    echo_tail_s: float = field(default_factory=lambda: _env_float("AVR_ECHO_TAIL", 0.35))

    # --- Grading ---
    # Defaults deliberately lenient. Speech recognition mangles words, people phrase answers
    # differently every time, and a false "incorrect" costs a real repetition of a card the
    # user actually knew. A false "correct" only costs one slightly-early interval, and the
    # spoken override window is there to catch it.
    fuzzy_correct: float = field(default_factory=lambda: _env_float("AVR_FUZZY_CORRECT", 0.62))
    fuzzy_wrong: float = field(default_factory=lambda: _env_float("AVR_FUZZY_WRONG", 0.30))
    ollama_url: str = field(
        default_factory=lambda: _env_str("AVR_OLLAMA_URL", "http://127.0.0.1:11434")
    )
    # qwen2.5:3b over the locally-present phi4: 9.1GB against a ~12.7GB working set is too slow
    # for a per-card fallback, and minimum latency is the headline requirement.
    ollama_model: str = field(default_factory=lambda: _env_str("AVR_OLLAMA_MODEL", "qwen2.5:3b"))
    judge_timeout_s: float = field(default_factory=lambda: _env_float("AVR_JUDGE_TIMEOUT", 12.0))

    # --- Session ---
    terminator: str = field(default_factory=lambda: _env_str("AVR_TERMINATOR", "done"))
    # 0 means advance the moment a card is graded, with no pause. Say "undo" afterwards to
    # take back the last grade — that is strictly better than making every single card wait
    # for a window that is usually not used.
    override_window_s: float = field(default_factory=lambda: _env_float("AVR_OVERRIDE_WINDOW", 0.0))
    # "auto"   — text similarity, with the local model deciding what similarity cannot.
    # "manual"  — never grades. Reads the answer out and waits for you to say good or again.
    #
    # These are the only two modes on purpose. There used to be a third, where the model was
    # off and similarity alone decided the ambiguous middle: it guessed, and it guessed badly.
    # With no model available the honest move is to ask the person, not to invent a verdict.
    grading_mode: str = "auto"

    # Flag applied to a card when you say skip or bury, so it can be found again later.
    # 0 = do not flag. Red (1) pairs with the anki-obsidian pipeline, which picks up
    # red-flagged cards for review.
    flag_on_skip: int = field(default_factory=lambda: int(_env_float("AVR_FLAG_ON_SKIP", 0)))

    # Headphones mode: nothing the computer says can reach the microphone, so the echo gate is
    # unnecessary and you can talk over it. Saying anything while a card is being read cuts the
    # speech off immediately — say "skip" the moment you recognise a card you cannot answer,
    # or start answering as soon as you know it, without waiting for the sentence to finish.
    headphones: bool = False

    # Spoken words per action. See DEFAULT_COMMAND_WORDS.
    command_words: dict = field(default_factory=lambda: {k: list(v) for k, v in DEFAULT_COMMAND_WORDS.items()})

    @property
    def manual(self) -> bool:
        return self.grading_mode == "manual"

    @classmethod
    def from_mapping(cls, data: dict) -> "Config":
        """Build from Anki's add-on config (config.json), falling back to defaults per key.

        The add-on has no environment to read, so this is how the settings dialog feeds the
        same Config object the CLI builds from environment variables.
        """
        defaults = cls()

        def pick(key: str, fallback):
            value = data.get(key)
            return fallback if value is None or value == "" else value

        return cls(
            whisper_bin=str(pick("whisper_binary", defaults.whisper_bin)),
            whisper_model=Path(str(pick("whisper_model", defaults.whisper_model))).expanduser(),
            say_voice=str(data.get("say_voice") or ""),
            say_rate=str(pick("say_rate", defaults.say_rate)),
            echo_tail_s=float(pick("echo_tail_seconds", defaults.echo_tail_s)),
            fuzzy_correct=float(pick("fuzzy_correct", defaults.fuzzy_correct)),
            fuzzy_wrong=float(pick("fuzzy_wrong", defaults.fuzzy_wrong)),
            ollama_url=str(pick("ollama_url", defaults.ollama_url)),
            ollama_model=str(pick("ollama_model", defaults.ollama_model)),
            judge_timeout_s=float(pick("judge_timeout_seconds", defaults.judge_timeout_s)),
            terminator=str(pick("terminator", defaults.terminator)),
            override_window_s=float(pick("override_window_seconds", defaults.override_window_s)),
            grading_mode=str(data.get("grading_mode") or "auto").lower(),
            flag_on_skip=int(data.get("flag_on_skip") or 0),
            headphones=bool(data.get("headphones", False)),
            command_words=_merge_command_words(data.get("command_words")),
        )

    def validate(self) -> list[str]:
        """Return human-readable problems. Empty list means good to go."""
        problems = []
        if not 0.0 <= self.fuzzy_wrong <= self.fuzzy_correct <= 1.0:
            problems.append(
                f"fuzzy thresholds must satisfy 0 <= wrong ({self.fuzzy_wrong}) "
                f"<= correct ({self.fuzzy_correct}) <= 1"
            )
        if not self.terminator.strip():
            problems.append("terminator keyword must not be empty")
        if self.override_window_s < 0:
            problems.append("override window must not be negative")
        if not 0 <= self.flag_on_skip <= 7:
            problems.append(f"flag must be 0-7, got {self.flag_on_skip}")
        for action, spoken in self.command_words.items():
            if not spoken:
                problems.append(f"the {action!r} command needs at least one word")
        if self.grading_mode not in ("auto", "manual"):
            problems.append(f"grading mode must be 'auto' or 'manual', got {self.grading_mode!r}")
        return problems
