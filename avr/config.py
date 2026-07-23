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
    fuzzy_correct: float = field(default_factory=lambda: _env_float("AVR_FUZZY_CORRECT", 0.75))
    fuzzy_wrong: float = field(default_factory=lambda: _env_float("AVR_FUZZY_WRONG", 0.40))
    ollama_url: str = field(
        default_factory=lambda: _env_str("AVR_OLLAMA_URL", "http://127.0.0.1:11434")
    )
    # qwen2.5:3b over the locally-present phi4: 9.1GB against a ~12.7GB working set is too slow
    # for a per-card fallback, and minimum latency is the headline requirement.
    ollama_model: str = field(default_factory=lambda: _env_str("AVR_OLLAMA_MODEL", "qwen2.5:3b"))
    judge_timeout_s: float = field(default_factory=lambda: _env_float("AVR_JUDGE_TIMEOUT", 12.0))

    # --- Session ---
    terminator: str = field(default_factory=lambda: _env_str("AVR_TERMINATOR", "done"))
    override_window_s: float = field(default_factory=lambda: _env_float("AVR_OVERRIDE_WINDOW", 2.5))

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
        return problems
