## v4

- `OLLAMA_CONTEXT_LENGTH` default lowered from `131072` to `32768` (#214). gemma4:12b's true native context is `262144` (not 131072); 32768 covers Hermes' ~4157-token system prompt plus a long conversation, loads gemma4:12b 100% on a 16 GB GPU at ~8.95 GB (vs ~10.3 GB at 131072), leaves ~6.6 GB headroom for the embedding model, and avoids the eviction race at an oversized window that was bouncing the box to `gemma4:e2b`.
- New `OLLAMA_FLASH_ATTENTION` variable (default `1`) — enables flash attention (prerequisite for optional KV-cache quant; negligible speed change on this GPU). Wired on both the `.kube` and GPU `.container` render paths.
- New `OLLAMA_EMBED_MODEL` variable (default `nomic-embed-text`), pre-pulled at install. Embeddings/RAG must target this tag, never the chat model: a distinct model gets its own llama-server runner that serves in parallel with a chat generation, so embeds don't serialize behind it (#214). Do NOT raise `OLLAMA_NUM_PARALLEL` or run a second Ollama.

## v3

- `OLLAMA_DEFAULT_MODEL` default bumped to `gemma4:12b` (newer 12B parameter natively multimodal model, with `gemma4:e2b` supported as a lighter alternative).
- `OLLAMA_EXTRA_MODELS` default changed to empty string (`""`) so only one model is loaded by default.

## v2

- New `OLLAMA_EXTRA_MODELS` variable (CSV) — additional models pre-pulled at install time on top of `OLLAMA_DEFAULT_MODEL`. Default ships a quantized 26B model that fits 100% on a 16 GB GPU, giving the operator a one-click "smarter but slower" choice in Hermes' Models tab without a fresh download (#1046). Note: the 26B-VRAM variant is text-only — multimodal stays on `gemma4:e4b`.
- `OLLAMA_DEFAULT_MODEL` default bumped from `gemma3:4b` to `gemma4:e4b` — same VRAM class, newer architecture, AND native multimodal (`ollama show gemma4:e4b` reports `completion, vision, audio, tools, thinking`). The default bump alone gets a fresh install most of the way through #923 / #938: vision-capable LLM on disk without needing a separate `OLLAMA_VISION_MODEL` pull.
- `OLLAMA_VISION_MODEL` demoted to "only set if you've changed the default to a text-only tag"; description updated accordingly.
- `pull_model` now verifies the tag is present in `/api/tags` *after* the streaming pull reports success. The CLI and the HTTP streaming endpoint both occasionally report success while manifest write fails silently (e.g. `library/<namespace>/` left root-owned by an earlier rootful run); the unverified happy path left operators with a `HTTP 404: model not found` on first chat. Now we fail loud (#1047).

## v1

- Initial template — single model pull (`OLLAMA_DEFAULT_MODEL`), optional vision model (`OLLAMA_VISION_MODEL`), GPU passthrough toggle.
