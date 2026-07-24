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
| `headphones` | With headphones on, you can talk over the card being read — say your answer or *skip* the moment you know. Leave off when using speakers, or it will transcribe its own voice. |
| `command_words` | The words for each action. Every action takes a list, so add your own: `{"skip": ["skip", "bury", "next"]}`. |
| `flag_on_skip` | 0-7. Also flag the card when you skip it. 1 is red. |
| `grading_mode` | `auto` grades for you. `manual` reads the answer back and waits for you to say good or again — no model needed. |
| `fuzzy_correct` | At or above this similarity, correct without asking the model. |
| `fuzzy_wrong` | Below this similarity, incorrect without asking the model. Between the two, the model decides. |
| `whisper_binary` | Name or absolute path. Homebrew locations are searched automatically. |
| `whisper_model` | Swap in `ggml-small.en.bin` for better accuracy and worse latency. |
| `say_voice` | Blank uses the system default. Any voice from System Settings → Accessibility → Spoken Content. |
| `say_rate` | Words per minute. |
| `echo_tail_seconds` | How long the microphone stays ignored after the computer stops speaking. Raise it if the add-on transcribes its own voice. |
| `ollama_url` / `ollama_model` / `judge_timeout_seconds` | The local grader. If Ollama is not running, ambiguous answers fall back to the fuzzy verdict and reviewing continues. |
