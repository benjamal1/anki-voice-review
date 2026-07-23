# T4 — Session state machine + CLI

**Blocked by:** T1, T2, T3
**Status:** todo

## Delivers

`avr review` — the full hands-free loop. This is the working product.

## Scope

- Session state machine consuming an **abstract stream of transcript events** plus a card
  record, emitting **intents** (speak, show answer, answer card with ease N, quit) rather than
  performing I/O itself. This is the project's primary test seam; keep it that way.
- Turn-taking: transcript lines accumulate into an answer buffer from the moment the question
  finishes being spoken. The terminator keyword (`done`) closes the buffer and triggers grading.
- Commands recognised **only as a whole utterance**, never as a substring, so a card whose
  answer contains the word "again" does not fire a command:
  `again`, `hard`, `good`, `easy`, `repeat`, `skip`, `quit`.
- Verdict flow: `guiShowAnswer`, speak the verdict, speak the correct answer when incorrect,
  then hold an override window. An ease command spoken in that window wins; otherwise correct
  submits Good and incorrect submits Again. Then `guiAnswerCard` and advance.
- Per-card timings printed at session end.
- Config object with in-code defaults, overridable by environment variable: model path,
  terminator keyword, fuzzy thresholds, override window, TTS voice and rate, Ollama model and
  endpoint, AnkiConnect URL.

## Acceptance criteria

1. A full session runs end to end on a real deck with no keyboard or mouse contact after launch.
2. Saying `done` closes the answer and triggers grading; a mid-answer pause does not.
3. Saying an ease word during the override window overrides the graded default.
4. Saying `repeat` re-speaks the current card without grading it.
5. Saying `skip` advances without submitting a grade.
6. Saying `quit` ends the session cleanly and terminates the `whisper-stream` child.
7. The state machine is unit-tested with scripted transcript sequences and **no** mic, speakers,
   Anki, or Ollama: answer accumulation, terminator detection, whole-utterance command matching,
   override precedence, echo-gate suppression, and the skip/repeat/quit paths.
