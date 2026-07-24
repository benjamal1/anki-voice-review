# Voice Review for Anki

Review Anki by voice. The card is read aloud, you say your answer, it grades you and moves on.
No keyboard, no mouse, no screen.

Everything runs locally on your Mac. Nothing is sent anywhere.

**Wear headphones.** The loop is always listening so you can speak the instant you know the
answer — which only works if the mic never hears the computer's own voice.

---

## What this add-on does and does not include

**The add-on is pure Python.** Anki add-ons cannot bundle native programs or large model
files, so this one does not ship a speech engine. It drives three things that live on your Mac:

| Piece | Where it comes from | Required? |
|---|---|---|
| **Reading cards aloud** | `say`, built into macOS | Included with macOS — nothing to install |
| **Hearing your answer** | **whisper.cpp**, installed separately via Homebrew, plus a ~141 MB model file you download once | **Required.** Without it there is no speech recognition and the add-on cannot work |
| **Grading your answer** | **Ollama**, installed separately | **Optional.** Only needed for Automatic mode. Manual mode needs no model at all |

If you install nothing, the add-on will tell you what is missing and how to get it —
**Tools → Voice Review → Settings**, top panel. It re-checks on demand and refuses to start a
review while something required is missing, rather than failing strangely mid-session.

---

## Setup

### 1. Install the add-on

Download `anki-voice-review.ankiaddon` from the
[latest release](https://github.com/benjamal1/anki-voice-review/releases), then in Anki:

**Tools → Add-ons → Install from file…** → pick the file → **restart Anki**.

Or build it yourself:

```sh
git clone https://github.com/benjamal1/anki-voice-review.git
cd anki-voice-review
python3 build_addon.py     # writes dist/anki-voice-review.ankiaddon
```

### 2. Install whisper.cpp (required)

This is the speech recogniser. It runs entirely on your machine.

```sh
brew install whisper-cpp
```

No Homebrew? Install it first from [brew.sh](https://brew.sh).

### 3. Download the speech model (required)

One file, about 141 MB, downloaded once.

```sh
mkdir -p ~/whisper-models
curl -L -o ~/whisper-models/ggml-base.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin
```

Want better accuracy and can accept a little more delay? Download `ggml-small.en.bin` from the
same place and point **Settings → Whisper model** at it.

### 4. Install Ollama (optional)

Only needed for **Automatic** grading. If you would rather grade your own answers, choose
**Manual** in Settings and skip this step entirely — nothing else is required.

In Automatic mode, text similarity settles the clear cases and Ollama judges the rest.

Download from [ollama.com](https://ollama.com), open the app, then:

```sh
ollama pull qwen2.5:3b
```

Typical delay when it is consulted: about half a second.

### 5. Grant microphone access

Open a deck, click **Study Now**, then **Tools → Voice Review** and press **Start**. macOS will
ask for microphone permission **on Anki's behalf** the first time. Allow it.

If you miss the prompt: **System Settings → Privacy & Security → Microphone → enable Anki**,
then restart Anki.

---

## Using it

Open a deck, click **Study Now**, then **Tools → Voice Review** (or `Ctrl+Shift+V`) and press
**Start**.

The card is read out. Say your answer, then say **"done"**. You hear "correct" or "incorrect",
and on a wrong answer the correct answer is read back. There is then a short window to change
the grade by voice before it is submitted.

| Say | What happens | **Stability** |
|---|---||---|
| your answer, then **done** | grade it, then straight on to the next card |---|
| **undo** (or *go back*) | take back the last grade. It says "undone" and waits — nothing is read out, just say how it should have been graded and it is applied to that card | Not tested reliably |
| just **done**, nothing before it | you didn't know it — marked wrong and the answer is read back |---|
| **again** / **hard** / **good** / **easy** | set the grade yourself. *yes*/*no*/*correct*/*right* also work |---|
| **repeat** | read the card again, discard what you had said | Not tested|---|
| **skip** or **bury** | set the card aside and move on. The answer is never shown or read — for image cards and anything that cannot be read aloud. Can also flag the card, see below |---|
| **quit** | end the session |---|

**Stop** ends the session immediately, cutting off mid-sentence if it is talking.

### If a grade looks wrong

The window shows what it actually heard next to every verdict. Most surprising grades are
mis-hearings, not mis-grading. If the transcript is right but the grade is genuinely wrong,
say **undo** and grade it yourself, or switch **Grading** to Manual so you grade every card.

---

## Settings

**Tools → Voice Review → Settings**, or **Tools → Add-ons → Voice Review → Config**.

| Setting | Default | What it does |
|---|---|---|
| End-of-answer word | `done` | the word that finishes your answer |
| Grading | Automatic | **Automatic** judges your answer with the model. **Manual** reads the answer back and waits for you to say good or again — no model, no time limit |
| Pause after grading | 0 s | 0 goes straight to the next card; say **undo** to take back a grade. Raise it to pause and wait instead |
| Flag on skip | off | also flag the card when you skip or bury it, so you can find it later with `flag:1` in the browser |
| Whisper model | `~/whisper-models/ggml-base.en.bin` | swap in `small.en` for accuracy over speed |
| Judge model | `qwen2.5:3b` | any model you have pulled in Ollama |
| Voice / Speech rate | system default / 190 | any voice from System Settings → Spoken Content |

---

## How grading works

Every spoken answer is judged by the local model (Ollama, `qwen2.5:3b`) for **meaning**, not
string similarity. Say the answer any way you like; if it means the same thing, it's correct.

The one shortcut: if what you said **is** the answer word-for-word (after ignoring case,
punctuation, and digit-vs-word — "four" matches "4"), it's marked correct instantly with no
model call. Everything else goes to the model, ~0.3s once it's warm.

There is no fuzzy string matching. It was removed: spoken answers vary too much for a
similarity threshold, and every threshold was a guess that mis-graded correct-but-reworded
answers.

If the model is unreachable, the answer is handed to you to grade rather than guessed.

**Latency:** ~0.3s per judged card once warm, and the model is warmed in the background at
session start. That is about the floor — the verdict is a single word, so latency is fixed
round-trip overhead, not model compute, and a smaller model is no faster (and less accurate).
`llama.cpp` is not faster because Ollama already runs on it.

**Manual mode** skips the model entirely — you grade every card yourself.

Cloze cards grade against the deleted text only. Card styling, scheduler widgets, and media
references are stripped before anything is spoken or graded.


## Troubleshooting

| Symptom | Cause |
|---|---|
| A spoken command does nothing | Whisper prefixes lines with a timestamp that used to break command matching — fixed. If it recurs, lower `vad_threshold`; short words like "skip" are the first thing a strict threshold misses. Enable the trace log (below) to see exactly what was recognised. |
| It transcribes its own voice / grades gibberish | You are on speakers. **Headphones are required** — the mic must not hear the computer. |
| Everything is graded incorrect | Check the transcript shown next to the verdict — if it is empty or garbled, whisper is not hearing you. Grant Anki microphone access. |
| Sits on "Listening…" and nothing happens | `whisper-stream` exited, almost always a denied microphone. The window will say so. |
| "Skip" says "buried 0 cards" | Another add-on (e.g. AJT Mortician) may be intercepting bury. The trace log records the bury count. |
| "Not ready" when pressing Start | Something required is missing — Settings shows which, and how to fix it. |

### Trace log

For diagnosing a live session, create an empty file named `debug` in the add-on folder
(`~/Library/Application Support/Anki2/addons21/anki_voice_review/`). The add-on then writes
`trace.log` beside it — every heard line, what it was recognised as, the phase, and each call
to Anki. Delete `debug` to turn it off.

---

## Latency

Per-card time is mostly transcription, then the model if it is consulted. What helps, roughly
in order:

| Change | Effect |
|---|---|
| `whisper_threads` (default 8) | Biggest single win. The whisper default of 4 leaves most of an M-series chip idle. |
| `announce_verdict: false` | Saves about a second per card. The verdict is on screen, and a wrong answer is still read back. |
| `whisper_length_ms` (default 5000) | Less trailing audio transcribed per utterance. Lower is faster; too low clips long answers. |
| `say_rate` (default 190) | Everything spoken gets shorter. 220-250 is still comfortable once used to it. |
| `ollama_keep_alive` (default 30m) | Keeps the judge loaded. Without it a card after an idle spell pays seconds to reload the model. |
| Answer / say a command early | You do not have to wait for the card to finish being read. The loop is always listening, so speak the moment you know — it cuts the reading off at once. |

The judge model is warmed in the background as the session starts, so the first card that needs
it does not pay for a cold load. Going bigger than `qwen2.5:3b` does not help: the verdict is a
single word, so latency is fixed round-trip overhead, not model compute — a 7B is slower for no
gain on a task a 3B already grades correctly.

Cutting further means a smaller whisper model, and `base.en` is already the small one — going
below it costs accuracy, which costs re-reviews.

## Development

```sh
uv sync
uv run pytest             # unit tests, no hardware or network needed
python3 build_addon.py    # build the .ankiaddon (checks Python 3.9 compatibility)
```

There is also a command-line version for testing without the add-on:

```sh
uv run avr doctor    # check every prerequisite
uv run avr peek      # what the grader sees for the current card
uv run avr grade "Paris" "the capital is Paris"
uv run avr selftest  # drive the whole loop against live Anki, no mic needed
```

`avr selftest` creates a throwaway deck, runs the real loop against it with a scripted
transcript, and deletes the deck. It is how the loop gets tested without a microphone.

**The add-on targets Python 3.9**, the version Anki bundles. The build refuses to package
anything that would not parse there. Anki work happens on the GUI thread; the voice loop runs
on a worker thread and marshals every reviewer call back via `mw.taskman.run_on_main`.

---

## Requirements

macOS, Anki 2.1.50+. Not available for AnkiMobile or AnkiDroid — the add-on system is
desktop-only.

Note types beyond Basic and Cloze are not specifically handled and may grade against more text
than intended.
