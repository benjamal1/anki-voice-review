# T3 — Grader

**Blocked by:** none
**Status:** todo

## Delivers

`avr grade "<card answer>" "<transcript>"` returns a verdict, routing through fuzzy matching
first and only reaching the local LLM judge when the fuzzy score lands in the ambiguous band.

## Scope

- Fuzzy stage: `difflib.SequenceMatcher` ratio over normalised strings. Above the upper
  threshold is correct, below the lower threshold is incorrect. Defaults 0.75 / 0.4, both
  configurable.
- Judge stage: local Ollama call to `qwen2.5:3b` at `http://127.0.0.1:11434`, receiving the
  question, the card's answer, and the transcript. Prompt constrains the reply to a single
  verdict word so parsing is trivial and a malformed reply is detectable.
  - `qwen2.5:3b` chosen over the already-present `phi4` — 9.1GB against a ~12.7GB working set
    is too slow for a per-card fallback, and the user's requirement is minimum latency.
- Fallbacks: if Ollama is unreachable or the reply does not parse, return the fuzzy verdict and
  log the degradation. Never crash the session.

## Acceptance criteria

1. An exact-match transcript grades correct without any LLM call.
2. A clearly wrong transcript grades incorrect without any LLM call.
3. A reworded-but-correct answer in the ambiguous band reaches the judge and grades correct.
4. With Ollama stopped, an ambiguous-band input still returns a verdict from fuzzy and logs the
   fallback.
5. A malformed judge reply is treated as the unreachable case, not propagated as an exception.
6. Fuzzy scoring and band routing are unit-tested with the LLM call stubbed.
