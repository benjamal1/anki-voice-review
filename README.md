# anki-voice-review

Hands-free voice review for Anki. Speak your answer, get graded, next card. No keyboard, no
mouse, no screen.

Everything runs locally on the Mac: Anki, the microphone, whisper.cpp, `say`, and Ollama.
Nothing leaves the machine and there is no cloud service anywhere in the loop.

## Requirements

macOS, plus:

| Thing | Install |
|---|---|
| Anki + AnkiConnect add-on | already installed |
| whisper.cpp | `brew install whisper-cpp` |
| A whisper model | `mkdir -p ~/whisper-models && curl -L -o ~/whisper-models/ggml-base.en.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin` |
| Ollama + a judge model | Ollama.app, then `ollama pull qwen2.5:3b` |

`say` is built into macOS. There are no Python dependencies.

```sh
uv sync
uv run avr doctor    # checks all of the above and tells you how to fix what's missing
```

## Use

Open Anki, pick a deck, click **Study Now** so a card is showing. Then, **in Terminal.app on
the Mac** (not over SSH — macOS will not grant microphone access to an SSH session):

```sh
uv run avr review
```

Speak your answer, then say **"done"**. You will hear "correct" or "incorrect", and on a wrong
answer the right answer is read back. There is then a brief window to override the grade by
voice before it is submitted.

| Say | Effect |
|---|---|
| *your answer*, then `done` | grade it |
| `again` / `hard` / `good` / `easy` | set the ease yourself — during the override window, or instead of answering |
| `repeat` | re-read the card, discard the partial answer |
| `skip` | next card, no grade |
| `quit` | end the session |

The first run will trigger a macOS microphone permission prompt. That is a one-time manual
step and cannot be scripted away.

### Other commands

```sh
uv run avr peek                        # what the grader sees for the current card
uv run avr peek --raw                  # ...plus the raw HTML
uv run avr grade "Paris" "it's Paris"  # grade a transcript, no mic needed
uv run avr listen                      # live transcription + echo gate test
```

## How grading works

Two stages, cheapest first, because latency is the point.

1. **Fuzzy match.** Similarity between what you said and the card's answer. Takes the better of
   whole-string similarity and word-window containment, so answering "the capital is Paris"
   against a card that says "Paris" is not punished for the extra words.
2. **LLM judge.** Only when the fuzzy score lands in the ambiguous middle band does a local
   `qwen2.5:3b` call decide. Measured at 0.3–0.6s warm. If Ollama is down or replies with
   something unparseable, the fuzzy verdict stands and the session carries on.

Most cards never reach stage 2.

Cloze cards grade against the deletion only, not the whole sentence — you are judged on the
hidden part, not the sentence you were just read. Basic cards grade against the back field.

## Configuration

Every knob is an environment variable with a sensible default. The ones worth touching:

| Variable | Default | What it does |
|---|---|---|
| `AVR_TERMINATOR` | `done` | the word that ends your answer |
| `AVR_FUZZY_CORRECT` | `0.75` | at or above this, correct without an LLM call |
| `AVR_FUZZY_WRONG` | `0.40` | below this, incorrect without an LLM call |
| `AVR_OVERRIDE_WINDOW` | `2.5` | seconds to countermand the grade by voice |
| `AVR_SAY_RATE` | `190` | speech rate |
| `AVR_OLLAMA_MODEL` | `qwen2.5:3b` | the judge |
| `AVR_WHISPER_MODEL` | `~/whisper-models/ggml-base.en.bin` | swap in `small.en` for better accuracy, worse latency |

## Design notes

**The echo gate.** The microphone is open for the whole session, so without special handling
the synthesised card audio gets transcribed as if you had said it — on every card, not as an
edge case. Two defences: `say` runs to completion before listening resumes, and the transcript
backlog is discarded afterwards, since whisper buffers audio and can emit lines from the TTS
after `say` has already exited.

**Commands match whole utterances only.** A card whose answer is "good cholesterol" must not
submit a grade halfway through your sentence.

**The state machine is the test seam.** `session.py` consumes transcript lines and emits
intents; it never touches audio, Anki, or the network. That is where the tests concentrate.
`runner.py` is the thin part that performs the I/O.

```sh
uv run pytest    # 101 tests, no hardware or network required
```

## Not supported

iOS/AnkiMobile (closed source, no AnkiConnect equivalent), note types beyond Basic and Cloze,
and any non-macOS host.
