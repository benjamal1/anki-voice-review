"""Speech-to-text: a long-lived `whisper-stream` process feeding a queue.

The process starts once per session, not once per card — spawning whisper per card would
reload the model every time and blow the latency budget, which is the whole point of the
project.

Kept behind a deliberately small interface (`start`, `stop`, `get`, `drain`) so the engine
can be swapped later without the session state machine knowing anything changed.
"""

from __future__ import annotations

import logging
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

# whisper-stream redraws its current line in place, so stdout carries terminal control codes.
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\r")
# Emitted for silence and non-speech audio. These are not things anyone said.
NOISE_LINES = {
    "[blank_audio]",
    "[silence]",
    "[ silence ]",
    "(silence)",
    "[music]",
    "(buzzing)",
    "[typing]",
}
NON_SPEECH = re.compile(r"^\s*[\[(][^\])]*[\])]\s*$")


def clean_line(raw: str) -> str:
    """Strip control codes and non-speech markers. Returns '' for anything not worth hearing."""
    text = ANSI.sub("", raw).strip()
    if not text:
        return ""
    if text.lower() in NOISE_LINES or NON_SPEECH.match(text):
        return ""
    return text


class TranscriberError(RuntimeError):
    pass


class Transcriber:
    """Wraps `whisper-stream`. Lines land in a queue via a background reader thread."""

    def __init__(self, binary: str, model: Path, language: str = "en") -> None:
        self._binary = binary
        self._model = model
        self._language = language
        self._process: subprocess.Popen[str] | None = None
        self._queue: queue.Queue[str] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._stopping = threading.Event()

    def preflight(self) -> None:
        """Fail loudly and specifically before a session starts, not with a raw traceback."""
        if shutil.which(self._binary) is None:
            raise TranscriberError(
                f"{self._binary!r} not found on PATH. Install whisper.cpp: brew install whisper-cpp"
            )
        if not self._model.exists():
            raise TranscriberError(
                f"Whisper model not found at {self._model}. Download it with:\n"
                f"  mkdir -p {self._model.parent} && curl -L -o {self._model} \\\n"
                "    https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"
            )

    def start(self) -> None:
        self.preflight()
        self._process = subprocess.Popen(
            [
                self._binary,
                "-m", str(self._model),
                "-l", self._language,
                # --step 0 switches from a fixed sliding window to VAD-driven emission: it
                # transcribes when you stop talking rather than every N milliseconds. Lower
                # latency per utterance and far less redundant re-transcription.
                "--step", "0",
                "--length", "8000",
                "-vth", "0.6",
                "--keep-context",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,  # model-load banner and Metal chatter, not transcript
            text=True,
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        assert self._process and self._process.stdout
        try:
            for raw in self._process.stdout:
                if self._stopping.is_set():
                    break
                text = clean_line(raw)
                if text:
                    self._queue.put(text)
        except (ValueError, OSError):
            pass  # stream closed under us during shutdown

    def get(self, timeout: float) -> str | None:
        """Next transcript line, or None if nothing was said within `timeout`."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> int:
        """Discard everything currently queued. Returns how many lines were dropped.

        This is the second half of the echo defence. whisper buffers audio, so lines produced
        *from* the TTS can still arrive after `say` has already exited — a wall-clock gate
        alone leaks them through. Draining after speaking throws away that backlog.
        """
        dropped = 0
        while True:
            try:
                self._queue.get_nowait()
                dropped += 1
            except queue.Empty:
                return dropped

    def stop(self) -> None:
        self._stopping.set()
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2)
        if self._reader and self._reader.is_alive():
            self._reader.join(timeout=1)
        self._process = None

    def __enter__(self) -> "Transcriber":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


class FakeTranscriber:
    """Scripted transcript source for tests and dry runs. Same interface, no mic."""

    def __init__(self, lines: list[str], delay: float = 0.0) -> None:
        self._lines = list(lines)
        self._delay = delay
        self.drained = 0

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def preflight(self) -> None: ...

    def get(self, timeout: float) -> str | None:
        if not self._lines:
            time.sleep(min(timeout, 0.01))
            return None
        if self._delay:
            time.sleep(self._delay)
        return self._lines.pop(0)

    def drain(self) -> int:
        self.drained += 1
        return 0
