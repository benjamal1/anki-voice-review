## Voice Review

Most people only need **Tools → Voice Review → Settings…**. These are the same values.

**Requirements** (macOS only):

```sh
brew install whisper-cpp
mkdir -p ~/whisper-models
curl -L -o ~/whisper-models/ggml-base.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin
ollama pull qwen2.5:3b        # optional, only for ambiguous answers
```

| Key | Meaning |
|---|---|
| `terminator` | The word that ends your answer and triggers grading. |
| `override_window_seconds` | 0 advances to the next card immediately after grading; say **undo** to take back the last grade. Set above 0 to pause and wait instead. |
| `headphones` | ON by default and recommended. With headphones the mic never hears the computer, so it cannot transcribe the card back over your answer. Turn off only if you review on speakers. |
| `barge_in` | Keep listening while a card is read so you can talk over it. Needs headphones. Off by default — enable once the basics feel solid. |
| `command_words` | The words for each action. Every action takes a list, so add your own: `{"skip": ["skip", "bury", "next"]}`. |
| `flag_on_skip` | 0-7. Also flag the card when you skip it. 1 is red. |
| `grading_mode` | `auto` grades for you. `manual` reads the answer back and waits for you to say good or again — no model needed. |
| `whisper_binary` | Name or absolute path. Homebrew locations are searched automatically. |
| `whisper_model` | Swap in `ggml-small.en.bin` for better accuracy and worse latency. |
| `say_voice` | Blank uses the system default. Any voice from System Settings → Accessibility → Spoken Content. |
| `say_rate` | Words per minute. Raise it to shave time off every card. |
| `announce_verdict` | Speak "correct"/"incorrect". Turn off to save about a second per card; a wrong answer is still read back. |
| `whisper_threads` | Threads for transcription. 8 suits an M-series chip; the default of 4 leaves most of it idle. |
| `whisper_length_ms` | How much trailing audio is transcribed per utterance. Lower is faster, but clips very long answers. |
| `vad_threshold` | Lower is more eager to treat quiet audio as speech. Raise it if background noise triggers it; lower it if short words like "skip" are missed. |
| `ollama_keep_alive` | How long the judge model stays loaded between cards. |
| `echo_tail_seconds` | How long the microphone stays ignored after the computer stops speaking. Raise it if the add-on transcribes its own voice. |
| `ollama_url` / `ollama_model` / `judge_timeout_seconds` | The local grader. If Ollama is not running, ambiguous answers fall back to the fuzzy verdict and reviewing continues. |
