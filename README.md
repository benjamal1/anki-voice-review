# Voice Review for Anki

Review Anki by voice. The card is read aloud, you say your answer, it grades you and moves on.
No keyboard, no mouse, no screen.

Everything runs locally on your Mac. Nothing is sent anywhere.

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

| Say | What happens |
|---|---|
| your answer, then **done** | grade it, then straight on to the next card |
| **undo** (or *go back*) | take back the last grade and put that card in front of you again |
| just **done**, nothing before it | you didn't know it — marked wrong and the answer is read back |
| **again** / **hard** / **good** / **easy** | set the grade yourself. *yes*/*no*/*correct*/*right* also work |
| **repeat** | read the card again, discard what you had said |
| **skip** or **bury** | set the card aside and move on. The answer is never shown or read — for image cards and anything that cannot be read aloud. Can also flag the card, see below |
| **quit** | end the session |

**Stop** ends the session immediately, cutting off mid-sentence if it is talking.

### If a grade looks wrong

The window shows what it actually heard next to every verdict. Most surprising grades are
mis-hearings, not mis-grading. If the transcript is right but the grade is wrong, lower
**Settings → Correct at or above**.

---

## Settings

**Tools → Voice Review → Settings**, or **Tools → Add-ons → Voice Review → Config**.

| Setting | Default | What it does |
|---|---|---|
| End-of-answer word | `done` | the word that finishes your answer |
| Headphones | off | let you talk over the card being read — say your answer or *skip* the moment you know |
| Pause after grading | 0 s | 0 goes straight to the next card; say **undo** to take back a grade. Raise it to pause and wait instead |
| Grading | Automatic | **Automatic** grades for you. **Manual** reads the answer back and waits for you to say good or again — no model needed, no time limit |
| Correct at or above | 0.62 | similarity needed to be marked correct outright. Lower = more lenient |
| Incorrect below | 0.30 | similarity below which it is marked wrong outright |
| Flag on skip | off | also flag the card when you skip or bury it, so you can find it later with `flag:1` in the browser |
| Whisper model | `~/whisper-models/ggml-base.en.bin` | swap in `small.en` for accuracy over speed |
| Judge model | `qwen2.5:3b` | any model you have pulled in Ollama |
| Voice / Speech rate | system default / 190 | any voice from System Settings → Spoken Content |

Grading is deliberately lenient. A wrong "incorrect" makes you repeat a card you knew; a wrong
"correct" costs one slightly early interval, and you can override it by voice.

---

## How grading works

Two stages, cheapest first.

1. **Text similarity.** Your words against the card's answer, taking the better of whole-answer
   similarity and best-phrase-within-the-sentence. So "the capital is Paris" scores full marks
   against a card that just says "Paris". Mathematical notation is expanded to how it is
   spoken, so `2^K` matches "two to the power of K".
2. **The local model**, only when similarity lands in the middle and cannot decide.

Most answers never reach stage 2.

If nothing can decide — the model is unavailable and similarity is inconclusive — the answer
is read back and **you** are asked to grade it. It is never guessed. An earlier version split
the middle band by score in that situation; that was a coin flip wearing a threshold, and it
produced verdicts that looked authoritative and were not.

### Headphones mode

On speakers, the add-on has to stop listening while it talks, or it transcribes its own voice
as your answer. That means waiting for the card to finish being read before you can say
anything.

With headphones nothing it says reaches the microphone, so **Settings → Headphones** lets you
interrupt: start answering as soon as you know it, or say *skip* the moment you recognise a
card you cannot do. Whatever you say cuts the speech off immediately.

### Changing the words

Every command accepts several words, and you can change them in
**Tools → Add-ons → Voice Review → Config** under `command_words`:

```json
"command_words": { "skip": ["skip", "bury", "next"], "good": ["good", "yes"] }
```

Actions you do not list keep their defaults.

### Flagging cards you skip

Set **Flag on skip** to a colour and saying *skip* or *bury* will flag the card as well as
setting it aside. Useful for marking cards that do not work by voice — image cards, anything
with a diagram, or a card whose wording needs fixing — and finding them later with a browser
search like `flag:1`.

It announces the colour so you know it registered.

### Manual mode

Set **Grading** to Manual. The card is read out, you answer, the answer is read back, and it
waits for you to say **good** or **again** — indefinitely, with no default and no timer. No
language model is involved at any point.

**Cloze cards** grade against the deleted text only, not the whole sentence — including
deletions inside MathJax, where Anki marks up nothing and the deletion has to be recovered by
comparing the two sides of the card.

Card styling, scheduling widgets like the FSRS Helper's status line, and media references are
stripped before anything is read aloud or graded.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Everything is graded incorrect | Usually the microphone. Check the transcript shown next to the verdict — if it is empty or garbled, whisper is not hearing you. Grant Anki microphone access. |
| Sits on "Listening…" and nothing happens | `whisper-stream` exited, almost always a denied microphone. The window will say so. |
| It transcribes its own voice | Raise **echo tail** in the add-on config. Headphones also solve it. |
| "Not ready" when pressing Start | Something required is missing — Settings shows which, and how to fix it. |
| Nothing happens after Stop then Start | Fixed in current versions. If it recurs, close and reopen the window. |

---

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
