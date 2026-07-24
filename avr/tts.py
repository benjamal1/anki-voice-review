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
import subprocess
import threading
import time
from typing import Optional, Protocol

from .stt import resolve_binary

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
        self._process: Optional[subprocess.Popen] = None
        self._cancelled = False
        self._lock = threading.Lock()

    def interrupt(self) -> None:
        """Cut off whatever is being said, right now.

        Pressing Stop mid-sentence should stop the sentence. `say` on a long card can run for
        many seconds, and without this the worker sits blocked inside it, ignoring the stop
        flag until it finishes — which also delays releasing the microphone, so a restart
        finds whisper still holding it.
        """
        with self._lock:
            self._cancelled = True
            process = self._process
        if process and process.poll() is None:
            process.terminate()

    def resume(self) -> None:
        """Clear a previous interrupt so the speaker can be used again."""
        with self._lock:
            self._cancelled = False

    def preflight(self) -> None:
        if resolve_binary("say") is None:
            raise SpeakerError(
                "`say` not found. This project is macOS-only; run it on the Mac where Anki lives."
            )

    def _command(self, text: str) -> list[str]:
        cmd = [resolve_binary("say") or "say"]
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
        with self._lock:
            if self._cancelled:
                return  # stop was pressed; do not start a new sentence

        self.muted_until = time.monotonic() + self._echo_tail_s
        try:
            # Popen rather than run() so interrupt() has something to terminate.
            process = subprocess.Popen(
                self._command(text), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
        except FileNotFoundError as exc:
            raise SpeakerError("`say` not found; this project runs on macOS only") from exc

        with self._lock:
            self._process = process
        try:
            process.wait()
        finally:
            with self._lock:
                self._process = None

        if process.returncode not in (0, -15, -9):  # -15/-9 are our own terminate/kill
            # A failed TTS call should not end a review session — the user can still read the
            # screen. Log it and carry on.
            stderr = (process.stderr.read() if process.stderr else b"") or b""
            log.warning("say failed: %s", stderr.decode(errors="replace").strip())

        with self._lock:
            if self._cancelled:
                return  # skip the echo tail; nothing is playing to echo

        # Audio keeps arriving at the mic for a moment after the process exits.
        remaining = self.muted_until - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        if gate is not None:
            dropped = gate.drain()
            if dropped:
                log.debug("echo gate discarded %d buffered line(s)", dropped)

    def start(self, text: str) -> None:
        """Begin speaking and return immediately, so the caller can keep listening.

        Used in headphones mode: nothing being said can reach the microphone, so there is no
        reason to stop listening while it talks — and every reason not to, since talking over
        it is the point.
        """
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            if self._cancelled:
                return
        try:
            process = subprocess.Popen(
                self._command(text), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except FileNotFoundError as exc:
            raise SpeakerError("`say` not found; this project runs on macOS only") from exc
        with self._lock:
            self._process = process

    @property
    def is_speaking(self) -> bool:
        with self._lock:
            process = self._process
        return process is not None and process.poll() is None

    def wait(self) -> None:
        with self._lock:
            process = self._process
        if process is not None:
            try:
                process.wait()
            except Exception:  # noqa: BLE001 - already terminated
                pass

    @property
    def is_muted(self) -> bool:
        return time.monotonic() < self.muted_until


class FakeSpeaker:
    """Records what would have been said. Same interface, no audio."""

    def __init__(self) -> None:
        self.said: list[str] = []
        self.muted_until = 0.0
        self.interrupted = False

    def interrupt(self) -> None:
        self.interrupted = True

    def resume(self) -> None:
        self.interrupted = False

    def start(self, text: str) -> None:
        self.speak(text)

    def wait(self) -> None: ...

    @property
    def is_speaking(self) -> bool:
        return False

    def preflight(self) -> None: ...

    def speak(self, text: str, gate: Drainable | None = None) -> None:
        text = (text or "").strip()
        if not text or self.interrupted:
            return
        self.said.append(text)
        if gate is not None:
            gate.drain()

    @property
    def is_muted(self) -> bool:
        return False
