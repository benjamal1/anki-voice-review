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
| `override_window_seconds` | How long you get to countermand the grade by voice before it is submitted. |
| `use_llm_judge` | Off makes grading purely mechanical — faster, but a correct answer worded differently from the card gets marked wrong. |
| `fuzzy_correct` | At or above this similarity, correct without asking the model. |
| `fuzzy_wrong` | Below this similarity, incorrect without asking the model. Between the two, the model decides. |
| `whisper_binary` | Name or absolute path. Homebrew locations are searched automatically. |
| `whisper_model` | Swap in `ggml-small.en.bin` for better accuracy and worse latency. |
| `say_voice` | Blank uses the system default. Any voice from System Settings → Accessibility → Spoken Content. |
| `say_rate` | Words per minute. |
| `echo_tail_seconds` | How long the microphone stays ignored after the computer stops speaking. Raise it if the add-on transcribes its own voice. |
| `ollama_url` / `ollama_model` / `judge_timeout_seconds` | The local grader. If Ollama is not running, ambiguous answers fall back to the fuzzy verdict and reviewing continues. |
