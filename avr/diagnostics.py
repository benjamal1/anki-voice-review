"""Check every external program the review loop depends on, and say how to fix each gap.

Nothing here ships with the add-on. Anki add-ons are pure Python — they cannot bundle native
binaries or a 141 MB model — so whisper.cpp and Ollama are separate installs that this code
shells out to. That makes "is it actually installed and working?" a question the user needs
answered in the UI, not discovered when grading silently misbehaves.

Shared by the CLI's `doctor` and the add-on's settings panel so both report the same thing.
"""

from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from .config import Config
from .stt import EXTRA_BIN_DIRS, resolve_binary

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # OK | WARN | FAIL
    detail: str = ""
    fix: str = ""
    required: bool = True

    @property
    def good(self) -> bool:
        return self.status == OK


def check_whisper(cfg: Config) -> Check:
    binary = resolve_binary(cfg.whisper_bin)
    if binary is None:
        return Check(
            "whisper.cpp",
            FAIL,
            f"{cfg.whisper_bin!r} not found",
            "Install it with:  brew install whisper-cpp\n"
            f"(Searched PATH and {', '.join(EXTRA_BIN_DIRS)}.)",
        )
    return Check("whisper.cpp", OK, binary)


def check_model(cfg: Config) -> Check:
    path = cfg.whisper_model
    if not path.exists():
        return Check(
            "Speech model",
            FAIL,
            f"not found at {path}",
            "Download it (about 141 MB):\n"
            f"  mkdir -p {path.parent}\n"
            f"  curl -L -o {path} \\\n"
            "    https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin",
        )
    size_mb = path.stat().st_size / 1_000_000
    if size_mb < 10:
        return Check(
            "Speech model",
            FAIL,
            f"{path.name} is only {size_mb:.1f} MB — the download was probably interrupted",
            f"Delete {path} and download it again.",
        )
    return Check("Speech model", OK, f"{path.name}, {size_mb:.0f} MB")


def check_say() -> Check:
    if resolve_binary("say") is None:
        return Check(
            "Speech output",
            FAIL,
            "`say` not found",
            "This add-on is macOS only — `say` is built into macOS.",
        )
    return Check("Speech output", OK, "macOS `say`")


def check_ollama(cfg: Config) -> Check:
    """Three distinct failures, three different fixes. The judge is optional."""
    try:
        with urllib.request.urlopen(f"{cfg.ollama_url}/api/tags", timeout=3) as response:
            payload = json.loads(response.read().decode())
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        installed = shutil.which("ollama") or _ollama_app_present()
        if not installed:
            return Check(
                "Grading model (optional)",
                WARN,
                "Ollama is not installed",
                "Reviewing works without it — answers are graded by text similarity alone,\n"
                "so a correct answer worded very differently may be marked wrong.\n"
                "To enable smarter grading, install Ollama from https://ollama.com\n"
                f"then run:  ollama pull {cfg.ollama_model}",
                required=False,
            )
        return Check(
            "Grading model (optional)",
            WARN,
            f"Ollama is installed but not responding at {cfg.ollama_url}",
            "Open the Ollama app (it needs to be running in the background).\n"
            "Reviewing still works without it, using text similarity alone.",
            required=False,
        )

    names = {m.get("name", "") for m in payload.get("models", [])}
    if cfg.ollama_model not in names:
        available = ", ".join(sorted(n for n in names if n)) or "none"
        return Check(
            "Grading model (optional)",
            WARN,
            f"{cfg.ollama_model} is not downloaded (available: {available})",
            f"Run:  ollama pull {cfg.ollama_model}\n"
            "Reviewing still works without it, using text similarity alone.",
            required=False,
        )
    return Check("Grading model (optional)", OK, f"{cfg.ollama_model} ready", required=False)


def _ollama_app_present() -> bool:
    from pathlib import Path

    return Path("/Applications/Ollama.app").exists()


def run_all(cfg: Config, include_anki: Optional[object] = None) -> list:
    """Every check, in the order they matter. `include_anki` is an optional bridge/client."""
    checks = [check_whisper(cfg), check_model(cfg), check_say(), check_ollama(cfg)]

    if include_anki is not None:
        checks.append(_check_anki(include_anki))
    return checks


def _check_anki(client: object) -> Check:
    try:
        card = client.preflight()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - the message is the whole point
        return Check("Anki reviewer", FAIL, str(exc), "Open a deck and click Study Now.")
    return Check("Anki reviewer", OK, f"card {card.card_id} showing")


def blocking_problems(checks: list) -> list:
    """Only the failures that actually stop a review from running."""
    return [c for c in checks if c.required and not c.good]
