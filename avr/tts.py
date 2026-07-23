"""Text-to-speech via macOS `say`, plus the echo gate.

No pre-generated cache. `say` speaks on-device in about the time it takes to start a process,
so a cache would buy a few milliseconds in exchange for a generation pass, a cache directory,
and audio that goes stale whenever a card is edited.

**The echo gate is the important part.** The microphone is open for the whole session, so
without this the speech synthesiser's own output gets transcribed as if the user had said it —
on every single card, not as an edge case. Two defences, because either alone leaks:

1. `say` is run to completion before listening resumes (a wall-clock gate).
2. The transcript queue is drained afterwards, since whisper buffers audio and can emit lines
   from the TTS *after* `say` has already exited.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from typing import Protocol

log = logging.getLogger(__name__)


class Drainable(Protocol):
    def drain(self) -> int: ...


class SpeakerError(RuntimeError):
    pass


class Speaker:
    def __init__(self, voice: str = "", rate: str = "190", echo_tail_s: float = 0.35) -> None:
        self._voice = voice
        self._rate = rate
        self._echo_tail_s = echo_tail_s
        self.muted_until = 0.0

    def preflight(self) -> None:
        if shutil.which("say") is None:
            raise SpeakerError(
                "`say` not found. This project is macOS-only; run it on the Mac where Anki lives."
            )

    def _command(self, text: str) -> list[str]:
        cmd = ["say"]
        if self._voice:
            cmd += ["-v", self._voice]
        if self._rate:
            cmd += ["-r", str(self._rate)]
        return cmd + ["--", text]

    def speak(self, text: str, gate: Drainable | None = None) -> None:
        """Say it, then hold the gate shut long enough for the echo to pass.

        Blocking on purpose: the user should not be answering over the top of the question,
        and letting `say` overlap the listening window is exactly what causes self-transcription.
        """
        text = (text or "").strip()
        if not text:
            return

        self.muted_until = time.monotonic() + self._echo_tail_s
        try:
            subprocess.run(self._command(text), check=True, capture_output=True)
        except FileNotFoundError as exc:
            raise SpeakerError("`say` not found; this project runs on macOS only") from exc
        except subprocess.CalledProcessError as exc:
            # A failed TTS call should not end a review session — the user can still read the
            # screen. Log it and carry on.
            log.warning("say failed: %s", exc.stderr.decode(errors="replace").strip())

        # Audio keeps arriving at the mic for a moment after the process exits.
        remaining = self.muted_until - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        if gate is not None:
            dropped = gate.drain()
            if dropped:
                log.debug("echo gate discarded %d buffered line(s)", dropped)

    @property
    def is_muted(self) -> bool:
        return time.monotonic() < self.muted_until


class FakeSpeaker:
    """Records what would have been said. Same interface, no audio."""

    def __init__(self) -> None:
        self.said: list[str] = []
        self.muted_until = 0.0

    def preflight(self) -> None: ...

    def speak(self, text: str, gate: Drainable | None = None) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.said.append(text)
        if gate is not None:
            gate.drain()

    @property
    def is_muted(self) -> bool:
        return False
