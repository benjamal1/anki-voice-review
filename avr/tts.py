"""Text-to-speech via macOS `say`.

Speech is **non-blocking and instantly interruptible**. `say(text)` queues an utterance and
returns immediately; a background thread speaks queued items in order. `interrupt()` clears the
queue and kills whatever is mid-sentence right now.

This is what lets sensing be decoupled from speaking: the review loop never blocks waiting for
a card to finish being read, so it is always free to hear a command, and the moment it does it
interrupts the speech. Requires headphones — the mic must not hear the `say` output, or it
would transcribe the card back to itself. Speakers mode was removed.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from typing import Optional

from .stt import resolve_binary

log = logging.getLogger(__name__)


class SpeakerError(RuntimeError):
    pass


class Speaker:
    def __init__(self, voice: str = "", rate: str = "190", **_ignored) -> None:
        self._voice = voice
        self._rate = rate
        self._queue: list[str] = []
        self._process: Optional[subprocess.Popen] = None
        self._cancel = False
        self._shutdown = False
        self._cond = threading.Condition()
        self._thread: Optional[threading.Thread] = None
        self.last_spoken = ""

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

    def say(self, text: str, gate=None) -> None:
        """Queue an utterance and return immediately. Speaks in order on a background thread."""
        text = (text or "").strip()
        if not text:
            return
        with self._cond:
            self._queue.append(text)
            self._cancel = False
            self.last_spoken = text
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True, name="avr-say")
                self._thread.start()
            self._cond.notify()

    # Blocking convenience for the CLI and one-off prompts. The loop uses say().
    def speak(self, text: str, gate=None) -> None:
        self.say(text)
        self.wait()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._cancel and not self._shutdown:
                    self._cond.wait()
                if self._shutdown:
                    return
                if self._cancel:
                    self._queue.clear()
                    self._cancel = False
                    continue
                text = self._queue.pop(0)
            try:
                process = subprocess.Popen(
                    self._command(text), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            except FileNotFoundError:
                log.error("`say` not found")
                return
            with self._cond:
                self._process = process
            process.wait()
            with self._cond:
                self._process = None

    def interrupt(self) -> None:
        """Stop speaking right now and drop anything queued. The core of fast barge-in."""
        with self._cond:
            self._cancel = True
            self._queue.clear()
            process = self._process
            self._cond.notify()
        if process and process.poll() is None:
            process.terminate()

    def resume(self) -> None:
        with self._cond:
            self._cancel = False

    @property
    def is_speaking(self) -> bool:
        with self._cond:
            process = self._process
        return process is not None and process.poll() is None

    @property
    def is_busy(self) -> bool:
        """Speaking now, or something still queued to speak.

        Queue-aware so it stays True in the gap between one utterance ending and the next
        Popen starting — that gap is where a "wait until done speaking" check would otherwise
        advance early, mid-answer.
        """
        with self._cond:
            if self._queue:
                return True
            process = self._process
        return process is not None and process.poll() is None

    def wait(self) -> None:
        """Block until the queue is empty and nothing is speaking. For the CLI/tests."""
        while True:
            with self._cond:
                idle = not self._queue and (self._process is None or self._process.poll() is not None)
            if idle:
                return
            with self._cond:
                self._cond.wait(timeout=0.05)

    def stop(self) -> None:
        with self._cond:
            self._shutdown = True
            self._cancel = True
            self._queue.clear()
            process = self._process
            self._cond.notify()
        if process and process.poll() is None:
            process.terminate()


class FakeSpeaker:
    """Records what would have been said. Same interface, no audio."""

    def __init__(self) -> None:
        self.said: list[str] = []
        self.last_spoken = ""
        self._interrupted = False

    def preflight(self) -> None: ...

    def say(self, text: str, gate=None) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.said.append(text)
        self.last_spoken = text

    def speak(self, text: str, gate=None) -> None:
        self.say(text)

    def interrupt(self) -> None:
        self._interrupted = True

    def resume(self) -> None:
        self._interrupted = False

    def wait(self) -> None: ...

    def stop(self) -> None: ...

    @property
    def is_speaking(self) -> bool:
        return False

    @property
    def is_busy(self) -> bool:
        return False
