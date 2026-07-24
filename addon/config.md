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
| `command_words` | The words for each action. Every action takes a list, so add your own: `{"skip": ["skip", "bury", "next"]}`. |
| `flag_on_skip` | 0-7. Also flag the card when you skip it. 1 is red. |
| `grading_mode` | `auto` grades for you. `manual` reads the answer back and waits for you to say good or again — no model needed. |
| `read_answer` | Auto mode only. `incorrect` reads the answer aloud only when you got it wrong, `always` every card, `never` shows it on screen without reading. Reading holds the advance until it finishes; a spoken command cuts it short. |
| `whisper_binary` | Name or absolute path. Homebrew locations are searched automatically. |
| `whisper_model` | Swap in `ggml-small.en.bin` for better accuracy and worse latency. |
| `say_voice` | Blank uses the system default. Any voice from System Settings → Accessibility → Spoken Content. |
| `say_rate` | Words per minute. Raise it to shave time off every card. |
| `announce_verdict` | Speak "correct"/"incorrect". Turn off to save about a second per card; the verdict is still on screen. |
| `whisper_threads` | Threads for transcription. 8 suits an M-series chip; the default of 4 leaves most of it idle. |
| `whisper_length_ms` | How much trailing audio is transcribed per utterance. Lower is faster, but clips very long answers. |
| `whisper_step_ms` | Transcription cadence. `500` = live captions: whisper re-transcribes every 500 ms and emits partial text as you speak, so the end-of-answer word lands ~1-2 s sooner. `0` = wait for a pause: transcribe only when you stop talking (steadier, but you feel the end-of-speech pause). Lower step = more responsive, more CPU. |
| `vad_threshold` | Lower is more eager to treat quiet audio as speech. Raise it if background noise triggers it; lower it if short words like "skip" are missed. |
| `ollama_keep_alive` | How long the judge model stays loaded between cards. |
| `ollama_url` / `ollama_model` / `judge_timeout_seconds` | The local grader. If Ollama is not running, ambiguous answers fall back to the fuzzy verdict and reviewing continues. |
