## v2

- New `OLLAMA_EXTRA_MODELS` variable (CSV) — additional models pre-pulled at install time on top of `OLLAMA_DEFAULT_MODEL`. Default ships a quantized 26B model that fits 100% on a 16 GB GPU, giving the operator a one-click "smarter but slower" choice in Hermes' Models tab without a fresh download (#1046). Note: the 26B-VRAM variant is text-only — multimodal stays on `gemma4:e4b`.
- `OLLAMA_DEFAULT_MODEL` default bumped from `gemma3:4b` to `gemma4:e4b` — same VRAM class, newer architecture, AND native multimodal (`ollama show gemma4:e4b` reports `completion, vision, audio, tools, thinking`). The default bump alone gets a fresh install most of the way through #923 / #938: vision-capable LLM on disk without needing a separate `OLLAMA_VISION_MODEL` pull.
- `OLLAMA_VISION_MODEL` demoted to "only set if you've changed the default to a text-only tag"; description updated accordingly.
- `pull_model` now verifies the tag is present in `/api/tags` *after* the streaming pull reports success. The CLI and the HTTP streaming endpoint both occasionally report success while manifest write fails silently (e.g. `library/<namespace>/` left root-owned by an earlier rootful run); the unverified happy path left operators with a `HTTP 404: model not found` on first chat. Now we fail loud (#1047).

## v1

- Initial template — single model pull (`OLLAMA_DEFAULT_MODEL`), optional vision model (`OLLAMA_VISION_MODEL`), GPU passthrough toggle.
