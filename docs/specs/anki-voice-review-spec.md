# Spec — Hands-Free Voice Review for Anki

> Supersedes `docs/handoff.md` wherever they disagree. The handoff's platform and STT
> assumptions were wrong; see [Corrections to the handoff](#corrections-to-the-handoff).

## Problem Statement

Reviewing Anki cards requires eyes on a screen and a hand on a keyboard. That rules out
reviewing while walking, cooking, driving, or doing anything else with your hands and eyes
occupied. The user wants to run a real Anki review session — real scheduling, real cards,
real grades written back — entirely by voice, with minimum latency between speaking and the
next card arriving.

## Solution

A single Python process on the Mac that drives a live Anki reviewer session:

1. It pulls the current card from Anki over AnkiConnect and speaks the question aloud.
2. The user speaks their answer.
3. A continuously-running `whisper-stream` process transcribes everything the mic hears, live.
4. When the user says the terminator keyword ("done"), the accumulated speech since the card
   started is taken as their answer.
5. The answer is graded against the card's real answer text — fuzzy match first, local LLM
   judge only when fuzzy is ambiguous.
6. The verdict is spoken. After a short override window, the grade is submitted to Anki and
   the next card is spoken.

Everything is local to the Mac. No cloud, no network hop, no other machine in the loop.

## Corrections to the handoff

These were established by probing the actual machines during the grill. The handoff is wrong
on each point and the spec follows reality, not the handoff.

| Handoff claim | Reality | Consequence |
|---|---|---|
| Desktop Anki runs on the OptiPlex | Anki.app runs on the **Mac**; AnkiConnect (addon `2055492159`) is installed there | Everything is Mac-local; AnkiConnect is reached at `http://127.0.0.1:8765` directly, **not** via the `ssh mac curl` transport that `anki-obsidian` uses |
| "Reuse the existing local Parakeet TDT 0.6B v3 (FluidAudio) pipeline" | **No such pipeline exists.** What exists is MacWhisper.app, a GUI watch-folder batch transcriber. Its `mw` CLI is a paid feature and is batch-only (`mw transcribe <file>`) | Parakeet/FluidAudio is dropped entirely. It is a Swift/CoreML macOS library, so it could in principle run on the Mac — but nothing is built, and building it is not on today's path |
| STT is an open problem needing new work | **whisper.cpp is already brew-installed on the Mac** — `whisper-stream`, `whisper-cli`, `whisper-server`, `whisper-vad-speech-segments`, with the Metal GPU backend loading on the M2 Pro | `whisper-stream` is the STT. Only a `ggml-base.en.bin` model download was needed |
| Ollama runs on the OptiPlex, CPU-only, so latency forces aggressive fuzzy-gating | **Ollama already runs on the Mac** (`ollama serve` live, Apple Silicon GPU) | The judge is GPU-accelerated and local. Fuzzy-gating is still kept, but for latency polish rather than survival |
| TTS should be pre-generated per card and cached | macOS `say` is built in and speaks on-device in ~100ms | The entire pre-generation pass, cache directory, and stale-audio-on-card-edit problem are deleted from scope |
| Open question: does the loop run on the same machine as Anki or across the network? | Mic, TTS, STT, LLM, and Anki are all physically on the Mac | Question is closed. Single process, single machine, no network in the hot path |

The handoff also **never addresses two things that turn out to be central**: how the loop knows
the user has finished speaking, and the fact that TTS audio echoes into the always-on mic. Both
are specified below.

## User Stories

1. As a reviewer, I want to start a session with one command, so that I can put the laptop down and begin.
2. As a reviewer, I want the card's question spoken aloud, so that I do not need to look at the screen.
3. As a reviewer, I want to speak my answer naturally, so that I am not constrained to exact wording.
4. As a reviewer, I want to say "done" to signal I have finished answering, so that a mid-answer pause does not cut me off.
5. As a reviewer, I want my answer graded against the card's real answer, so that the grade reflects whether I knew it.
6. As a reviewer, I want a reworded but semantically correct answer marked correct, so that I am not punished for not memorising phrasing.
7. As a reviewer, I want the verdict spoken back, so that I know how I was graded without looking.
8. As a reviewer, I want the correct answer spoken when I get it wrong, so that I actually learn the card.
9. As a reviewer, I want a brief window to override the verdict by voice, so that a wrong grade never silently corrupts my scheduling.
10. As a reviewer, I want to say "again", "hard", "good", or "easy" to set the ease directly, so that I keep final authority over scheduling.
11. As a reviewer, I want to say "repeat" to hear the card again, so that I can recover from mishearing it.
12. As a reviewer, I want to say "skip" to move past a card without grading it, so that a broken or unanswerable card does not stall the session.
13. As a reviewer, I want to say "quit" to end the session cleanly, so that I do not have to touch the machine to stop.
14. As a reviewer, I want the loop to ignore the TTS audio it just played, so that the system does not transcribe itself and grade my answer as gibberish.
15. As a reviewer, I want cloze cards to grade against the cloze deletion only, so that I am judged on the hidden part rather than the sentence I was just read.
16. As a reviewer, I want basic cards to grade against the back field, so that ordinary two-sided cards work with no configuration.
17. As a reviewer, I want the grade written into Anki's real scheduler, so that the session counts as a genuine review.
18. As a reviewer, I want minimum latency between saying "done" and the next card starting, so that a session does not feel like waiting.
19. As a reviewer, I want a clear error if Anki is not running or not in the reviewer, so that I am not left talking to a dead process.
20. As a reviewer, I want the session to work on speakers or headphones, so that I am not silently broken by an unstated hardware assumption.
21. As a reviewer, I want per-card timing printed at the end, so that I can see where latency actually goes.
22. As a reviewer, I want the fuzzy threshold and ambiguous band configurable, so that I can tune grading strictness without editing code.
23. As a developer, I want the STT engine behind a thin interface, so that Parakeet or another backend can be swapped in later without touching the loop.
24. As a developer, I want the loop's decision logic testable without a mic, speakers, Anki, or Ollama, so that the state machine can be covered by fast unit tests.

## Implementation Decisions

### Topology

Single Python process, Mac-local. Modules: an AnkiConnect client, a card-text extractor, an STT
adapter, a TTS adapter, a grader, and the session state machine. Dev and version control happen
on the OptiPlex; execution is Mac-only.

### AnkiConnect

Direct HTTP `POST http://127.0.0.1:8765`. Not the `ssh mac curl` transport — that exists in
`anki-obsidian` only because that tool runs on the OptiPlex, which is not the case here.

The loop drives the GUI reviewer, so Anki must be running with a deck open in the reviewer.
Actions used: `guiCurrentCard` to read the card, `guiShowAnswer` to reveal, `guiAnswerCard` to
submit an ease. A startup preflight pings AnkiConnect and calls `guiCurrentCard`; failure of
either aborts with a specific message distinguishing "Anki not running" from "Anki running but
not in the reviewer".

### Card text extraction

`guiCurrentCard` returns rendered `question` and `answer` HTML. No per-deck configuration and no
note-type assumptions beyond these two shapes:

- **Cloze** — the rendered answer contains the revealed deletion inside `span.cloze`. The
  grading target is the concatenated text of those spans. Detected by the presence of the span.
- **Basic** — Anki's rendered answer is the front, then an `<hr id=answer>` separator, then the
  back. The grading target is the text after that separator.

Both paths then strip HTML tags, drop `[sound:...]` and media references, collapse whitespace,
and normalise case and punctuation before comparison. Extraction is a pure function over the two
HTML strings, which makes it directly unit-testable against captured real card HTML.

### STT

A long-lived `whisper-stream` subprocess started once per session, reading the default input
device and writing transcript lines to stdout, which the loop reads line by line on a background
thread into a queue. Started with `--step 0` so it emits on VAD-detected utterance end rather
than on a fixed window, `-l en`, `--keep-context`, and the `ggml-base.en.bin` model. The process
is wrapped behind a small interface exposing "start", "stop", and "iterate transcript lines", so
the engine can be replaced without the state machine knowing.

### TTS and the echo gate

`say` as a subprocess, spoken live, no cache. Because the mic is always open, `say` output would
otherwise be transcribed as user speech on every single card. The adapter therefore sets a
suppression flag for the duration of the `say` call plus a short tail, and the loop discards any
transcript line that arrives while the flag is set. This is software-only and correct on speakers
or headphones; no hardware assumption is made.

### Turn-taking protocol

Always-on stream with a terminator keyword. Transcript lines accumulate into an answer buffer
from the moment the card's question finishes being spoken. Saying the terminator ("done") closes
the buffer and triggers grading. Command words are recognised only as a whole utterance, not as
substrings of an answer, so that a card whose answer legitimately contains the word "again" does
not fire a command. Commands: `again`, `hard`, `good`, `easy`, `repeat`, `skip`, `quit`.

### Grading

Two-stage, cheapest first:

1. **Fuzzy** — `difflib.SequenceMatcher` ratio between the normalised transcript and the
   normalised card answer. Above the upper threshold is correct, below the lower threshold is
   incorrect.
2. **LLM judge** — only when the ratio falls in the ambiguous band. A local Ollama call
   (`qwen2.5:3b`, chosen over the already-present `phi4` because 9.1GB against a ~12.7GB working
   set is too slow for a per-card fallback) receives the question, the card's answer, and the
   transcript, and returns a correct/incorrect verdict. The prompt constrains the reply to a
   single token-level verdict so parsing is trivial and a malformed reply is detectable.

If Ollama is unreachable or the reply does not parse, the grader falls back to the fuzzy verdict
and logs it rather than crashing the session.

Thresholds (upper, lower) live in config, defaulting to the handoff's 0.75 and 0.4.

### Verdict, override window, and ease mapping

After grading, the loop calls `guiShowAnswer`, speaks the verdict, and speaks the correct answer
when the verdict is incorrect. It then waits a short override window. If an ease command is
spoken in that window it wins; otherwise the default mapping applies — correct submits Good,
incorrect submits Again. `guiAnswerCard` is then called with the resulting ease and the loop
advances.

### Configuration

A single config object with defaults in code, overridable by environment variable: model path,
terminator keyword, fuzzy thresholds, override window duration, TTS voice and rate, Ollama model
and endpoint, AnkiConnect URL.

## Testing Decisions

A good test here asserts on externally observable behaviour — what the loop decides to do given
what it heard — and never on internal call ordering or private helpers.

The **primary seam is the session state machine**. It is written to consume an abstract stream of
transcript events and a card record, and to emit intents (speak, show answer, answer card with
ease N, quit) rather than performing I/O itself. Tests drive it with scripted transcript
sequences and assert on the emitted intents, with no mic, speakers, Anki, or Ollama involved.
This is one seam covering the whole decision surface, which is the ideal the project should hold
to as it grows.

Modules under test:

- **State machine** — answer accumulation, terminator detection, whole-utterance command
  matching, override window precedence over the default mapping, echo-gate suppression, and the
  skip/repeat/quit paths.
- **Card text extraction** — pure function over captured real `guiCurrentCard` HTML for both a
  Basic and a Cloze card, including HTML stripping and media-reference removal.
- **Grader** — fuzzy scoring and band routing, with the LLM call stubbed; plus the
  Ollama-unreachable and malformed-reply fallback paths.

The adapters (AnkiConnect HTTP, `whisper-stream` subprocess, `say` subprocess, Ollama HTTP) are
thin by design and are covered by a live preflight rather than by mocked unit tests, since
mocking a subprocess boundary tests the mock rather than the integration.

There is no prior art in this repository — it is empty apart from the handoff — so the test suite
establishes the conventions rather than following them.

## Out of Scope

- iOS / AnkiMobile in any form. Confirmed closed-source with no AnkiConnect equivalent.
- The OptiPlex. It is the development box; no runtime component touches it.
- Parakeet TDT / FluidAudio. Dropped for v1; the STT interface exists so it can return later.
- Pre-generated TTS caching, and the AwesomeTTS / `anki-handsfree` export pattern.
- An Anki add-on. The external-process-plus-AnkiConnect route is taken instead, so nothing needs
  installing into Anki beyond the AnkiConnect that is already there.
- Per-card LLM-authored grading criteria stored as a note field (the handoff's option 2), which
  was already deprioritised there and stays out.
- Per-deck configuration of the answer field, made unnecessary by the Basic/Cloze detection above.
- Image-occlusion and other note types beyond Basic and Cloze.
- Any cloud service.
- A GUI, a daemon, or autostart. It is a foreground command.

## Further Notes

**Preflight requirements at session start:** Anki running with a deck open in the reviewer;
AnkiConnect responding on `127.0.0.1:8765`; `ggml-base.en.bin` present; `ollama serve` up with
the judge model pulled; microphone permission granted to the terminal running the process. The
first `whisper-stream` run may trigger a macOS microphone permission prompt, which is a one-time
manual step that cannot be automated away.

**Latency is the headline requirement.** The design choices that serve it: a persistent
`whisper-stream` process rather than per-card spawn; a 3B judge rather than phi4; fuzzy-first so
most cards never reach the LLM at all; live `say` rather than a cache lookup; and no network hop
anywhere. Per-card timings are printed at session end so the assumption can be checked against
measurement rather than trusted.

**Known accepted risk:** `base.en` will mis-transcribe technical vocabulary and proper nouns,
which will show up as false-incorrect grades on exactly the cards that matter most. The mitigation
is the spoken verdict plus the override window, which keeps a bad transcription from silently
corrupting scheduling. If it proves annoying, the upgrade path is `small.en` first, then
revisiting Parakeet behind the STT interface.
