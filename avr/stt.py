"""Speech-to-text: a long-lived `whisper-stream` process feeding a queue.

The process starts once per session, not once per card — spawning whisper per card would
reload the model every time and blow the latency budget, which is the whole point of the
project.

Kept behind a deliberately small interface (`start`, `stop`, `get`, `drain`) so the engine
can be swapped later without the session state machine knowing anything changed.
"""

from __future__ import annotations

import logging
import os
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

# whisper-stream's own chatter, not speech. In VAD mode (--step 0) every utterance is wrapped:
#     ### Transcription 1 START | t0 = 0 ms | t1 = 3000 ms
#     the capital is paris
#     ### Transcription 1 END
# Passing those markers through appends "Transcription 1 START t0 0 ms" to every answer, which
# wrecks the fuzzy score and pushes every card to the judge or to a wrong verdict.
CONTROL_LINE = re.compile(r"^\s*(###|\[Start speaking\]|whisper_|main:|init:)", re.IGNORECASE)

# base.en emits these on silence — an artifact of its training data, not something you said.
# Only ever matched as a whole line, so a card whose answer really is "you" still works when
# spoken as part of a sentence.
HALLUCINATED_SILENCE = {
    "you",
    "thank you.",
    "thank you",
    "thanks for watching!",
    "thanks for watching.",
    "bye.",
    ".",
}


# A GUI app launched from Finder inherits a minimal PATH — no /opt/homebrew/bin — so inside the
# Anki add-on `shutil.which("whisper-stream")` finds nothing even though it is installed. Look in
# the usual Homebrew locations too rather than making the user hand-edit an absolute path.
EXTRA_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")


def resolve_binary(name: str) -> str | None:
    """Absolute path to `name`, searching PATH and the standard Homebrew prefixes."""
    if os.path.isabs(name):
        return name if os.access(name, os.X_OK) else None
    found = shutil.which(name)
    if found:
        return found
    for directory in EXTRA_BIN_DIRS:
        candidate = os.path.join(directory, name)
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def clean_line(raw: str) -> str:
    """Strip control codes and non-speech markers. Returns '' for anything not worth hearing."""
    text = ANSI.sub("", raw).strip()
    if not text:
        return ""
    if CONTROL_LINE.match(text):
        return ""
    lowered = text.lower()
    if lowered in NOISE_LINES or lowered in HALLUCINATED_SILENCE:
        return ""
    if NON_SPEECH.match(text):
        return ""
    return text


class TranscriberError(RuntimeError):
    pass


class Transcriber:
    """Wraps `whisper-stream`. Lines land in a queue via a background reader thread."""

    def __init__(
        self,
        binary: str,
        model: Path,
        language: str = "en",
        threads: int = 8,
        length_ms: int = 5000,
        vad_threshold: float = 0.45,
    ) -> None:
        self._binary = binary
        self._resolved_binary = binary
        self._model = model
        self._language = language
        self._threads = threads
        self._length_ms = length_ms
        self._vad_threshold = vad_threshold
        self._process: subprocess.Popen[str] | None = None
        self._queue: queue.Queue[str] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._stopping = threading.Event()

    def preflight(self) -> None:
        """Fail loudly and specifically before a session starts, not with a raw traceback."""
        resolved = resolve_binary(self._binary)
        if resolved is None:
            raise TranscriberError(
                f"{self._binary!r} not found. Install whisper.cpp: brew install whisper-cpp"
            )
        self._resolved_binary = resolved
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
                self._resolved_binary,
                "-m", str(self._model),
                "-l", self._language,
                # --step 0 switches from a fixed sliding window to VAD-driven emission: it
                # transcribes when you stop talking rather than every N milliseconds. Lower
                # latency per utterance and far less redundant re-transcription.
                "--step", "0",
                "--length", str(self._length_ms),
                "-vth", str(self._vad_threshold),
                "-t", str(self._threads),
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

    def is_alive(self) -> bool:
        """False once the whisper process has exited.

        Worth checking every tick: if the microphone is denied — which is exactly what happens
        the first time before the macOS permission prompt is answered — whisper-stream exits
        immediately. Without this the UI sits on "Listening…" forever and looks like the mic
        simply is not picking anything up.
        """
        return self._process is not None and self._process.poll() is None

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

    def is_alive(self) -> bool:
        return True
