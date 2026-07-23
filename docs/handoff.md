# Hands-Free Voice Review for Anki — Project Handoff

## Goal

Build a fully hands-free Anki review loop:

1. Card is shown → **answer text is spoken via pre-generated TTS** (not live).
2. User speaks their answer out loud.
3. Local STT transcribes it.
4. A grader compares the transcript to the card's back-side text and decides correct / incorrect.
5. The result is sent back into Anki as a review grade (Again/Good/etc.) via keystroke/API, and the next card comes up.
6. No mouse, keyboard, or screen required during a review session.

This is a **desktop-only** project. AnkiMobile (iOS) is closed-source with no plugin system and no AnkiConnect equivalent — confirmed via research, not an assumption. Do not attempt to build any part of this for iOS beyond, at most, TTS embedded in a card template's JS (unproven for STT). Target platform is desktop Anki running on the OptiPlex (Manjaro/Docker host) or wherever Anki desktop runs.

## Constraints / environment

- All processing must be **local/offline** — no cloud STT/TTS/LLM APIs. This follows an existing self-hosted-infrastructure philosophy (Nextcloud, Headscale/WireGuard, no external dependencies).
- STT: **Parakeet TDT 0.6B v3 (FluidAudio)** — already has a working local pipeline for this from a separate transcription project. Reuse that pipeline/model rather than standing up something new (e.g. Vosk, which is what the closest prior art uses).
- LLM grading (if used): **Ollama**, already planned/running on the OptiPlex (CPU-only — expect latency, see Grading section).
- Control glue: **AnkiConnect** (desktop Anki add-on, HTTP API on localhost). This is how virtually every existing Anki automation tool talks to a running Anki instance — use it rather than simulating keystrokes at the OS level.
- TTS: does not need to be real-time; can be pre-generated per-card and cached, similar to how the `anki-handsfree` add-on exports card audio for offline playback.

## Prior art to read before writing anything

Don't reinvent these — read the source, borrow patterns, and only write new code where nothing exists yet.

| Project | What it does | Relevance |
|---|---|---|
| [`williamknows/anki-voice`](https://github.com/williamknows/anki-voice) | Standalone Python tool: offline STT (Vosk) + TTS (pyttsx3) + AnkiConnect to review cards by voice command | Closest existing match to this whole pipeline. Swap Vosk → Parakeet, swap pyttsx3 → pre-generated TTS cache. |
| AnkiConnect | Local HTTP API for controlling a running desktop Anki instance (`guiShowAnswer`, `guiAnswerCard`, etc.) | The control layer everything below sits on. |
| [`angel333/anki-handsfree`](https://github.com/angel333/anki-handsfree) | Exports card audio to files for offline hands-free listening; pairs with AwesomeTTS | Pattern for pre-generating and caching TTS instead of doing it live. |
| [`abdnh/anki-asr`](https://github.com/abdnh/anki-asr) | Transcribes audio into a note field via pluggable ASR providers, caches results | Pattern for a pluggable/cacheable transcription backend — useful reference even though it's not built for live grading. |
| Anki-SmartReviewPad | Add-on doing "automatic answer checking" via DOM hooking in the review screen | Reference for hooking into Anki's review lifecycle events if going the add-on route instead of AnkiConnect-external-script route. |
| AnkiWeb "Type Answer Analysis AI" / "Answer Evaluation" add-ons | Existing LLM-graded typed-answer checking | Same grading problem as ours (answer semantically-but-not-exactly matches), just typed instead of spoken. Worth pulling source for the grading logic. |
| Anki's built-in "type in the answer" feature | Already does diff-based fuzzy comparison (the yellow/green highlighting) | May be directly reusable as the fuzzy-match comparator instead of writing a new one. |

## Architecture decision: grading approach

Discussed three options; landed on a staged approach rather than picking one permanently:

1. **Fuzzy string match** (e.g. Python `difflib.SequenceMatcher` / Ratcliff-Obershelp, threshold ~0.75) against the back-of-card text. Cheap, fast, no per-card authoring needed. Anki's own built-in typed-answer comparator already implements something like this — check whether it can be called directly before writing a new one.
2. **LLM-preprocessed per-card criteria** — an LLM reads each card ahead of time and writes down what a "correct" spoken answer must semantically contain, stored as a new card property. More accurate but adds an authoring/maintenance burden (new field, has to stay in sync when cards are edited). **Deprioritized** — not the recommended default.
3. **Live LLM judge (recommended simplest version)** — at review time, send `{question, back-of-card text, transcribed spoken answer}` to local Ollama and ask for a correct/incorrect verdict. No new card property, no preprocessing pass, grades against whatever's already on the card as-is.

**Recommended default:** fuzzy match first; only fall through to the Ollama judge call when the fuzzy score is ambiguous (e.g. in some middle band like 0.4–0.75). This avoids an LLM call on every single card (latency matters since Ollama here is CPU-only) while still catching semantically-correct-but-differently-worded answers that pure string matching would mark wrong.

## Suggested build order

1. Get AnkiConnect installed and confirm `guiShowAnswer` / `guiAnswerCard` work against a real deck from a script.
2. Wire the existing Parakeet pipeline in as the STT step, replacing Vosk in the `anki-voice` reference project.
3. Build the pre-generated TTS cache (per-card audio files, generated once and reused — same pattern as `anki-handsfree`).
4. Implement the fuzzy-match grader first (cheapest to test end-to-end).
5. Add the Ollama-judge fallback for ambiguous fuzzy scores.
6. Wire voice *commands* (not just answers) — "again," "good," "hard," "easy," "repeat" — through the same STT step so the whole loop, including grading overrides, is voice-only.

## Open questions for implementation

- Where does the STT/TTS/grading loop run relative to Anki itself — same machine as desktop Anki, or a separate process on the OptiPlex talking to AnkiConnect over the network?
- What's the acceptable latency budget per card, given Ollama is CPU-only on the OptiPlex? This determines how aggressive the fuzzy-match-first gating needs to be.
- Should the fuzzy-match threshold and ambiguous-band bounds be global config or per-deck?
