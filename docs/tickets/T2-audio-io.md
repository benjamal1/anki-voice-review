# T2 — Audio I/O + echo gate

**Blocked by:** none
**Status:** todo

## Delivers

`avr listen` starts a persistent `whisper-stream`, prints transcript lines live, and speaks a
test phrase through `say` — demonstrating that the echo gate suppresses the TTS from being
transcribed back as user speech.

## Scope

- STT adapter wrapping a long-lived `whisper-stream` subprocess behind a thin interface
  (`start`, `stop`, iterate transcript lines), so the engine can be swapped later.
  Flags: `--step 0` (VAD-driven emission, not fixed window), `-l en`, `--keep-context`,
  `-m ~/whisper-models/ggml-base.en.bin`.
- Background reader thread draining stdout into a queue; the caller never blocks on the process.
- TTS adapter wrapping `say`. Live, no cache.
- **Echo gate:** the TTS adapter raises a suppression flag for the duration of the `say` call
  plus a short tail; transcript lines arriving while the flag is set are discarded. Software
  only — must be correct on speakers, not just headphones.

## Acceptance criteria

1. `avr listen` prints transcript lines within roughly a second of speech ending.
2. Speaking into the mic while the gate is closed yields no transcript lines.
3. The test phrase spoken by `say` does **not** appear in the printed transcript.
4. Killing the process cleanly terminates the `whisper-stream` child — no orphans.
5. A missing model file fails at startup with a clear message, not a subprocess traceback.

## Notes

The first run may trigger a macOS microphone permission prompt for the terminal. One-time,
manual, cannot be automated.
