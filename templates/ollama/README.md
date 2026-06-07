# Ollama (Local LLM Server)

[Ollama](https://ollama.com/) is a single-binary local LLM runtime
that speaks an OpenAI-compatible HTTP API. ServiceBay's `ollama`
template wraps the upstream image
(`docker.io/ollama/ollama:latest`) as a hostNetwork pod bound to
`127.0.0.1` so other templates on the same host (e.g. `hermes`)
can reach it at `http://127.0.0.1:11434` without DNS or bridge
networking.

## Variables

- `OLLAMA_PORT` — host port. Default `11434`. Bound to loopback.
- `OLLAMA_DEFAULT_MODEL` — primary model. The tag Hermes' `model.model`
  points at after install. Default `gemma4:12b` — 12B params,
  fits on a 16 GB GPU, AND ships native multimodal
  (`ollama show gemma4:12b` reports `completion, vision, audio, tools,
  thinking`). That single default backs Solilos's multimodal-ingestion
  path without a separate vision pull. Any Ollama library tag works,
  plus user-namespaced tags like `gemma4:12b`, `gemma4:e2b`, or `VladimirGav/gemma4-26b-16GB-VRAM:latest`.
- `OLLAMA_EXTRA_MODELS` — comma-separated list of additional models
  pre-pulled at install time on top of the default. Gives the operator
  one-click switchable choices in Hermes' Models tab without a fresh
  download. Default is empty (set to empty string to skip, which ensures
  only one model is loaded by default). Set to `gemma4:e2b` or `VladimirGav/gemma4-26b-16GB-VRAM:latest`
  if you want a second model. **Caveat:** the 16
  GB quant of the 26B-VRAM variant strips vision and audio from its `Capabilities` block — it's
  text-only. Switching the active model to it drops multimodal; switch
  back to `gemma4:12b` or `gemma4:e2b` (or pull plain `gemma4:26b` ~17 GB with partial
  CPU offload) for OCR / voice-note flows.
- `OLLAMA_VISION_MODEL` — historical, mostly unused now that the
  default `gemma4:12b` ships vision + audio natively. Set this only if
  you've changed `OLLAMA_DEFAULT_MODEL` to a text-only tag and still
  want a vision backend for Solilos's `media-ingestion-multimodal` skill.
  Suggested non-default tags: `qwen2.5vl:7b`, `llava:13b`, `bakllava:7b`.
- `OLLAMA_CONTEXT_LENGTH` — Ollama's default load context window, in
  tokens. Default `131072` (gemma4:12b's full native context, ~6.5 GB
  VRAM, 100% GPU). Forces the size models load at so Hermes' 4157-token
  system prompt isn't truncated to a 1-token reply by Ollama's stock
  4096 default (#146 — the `/v1` endpoint ignores per-request
  `num_ctx`, so only this env-set default lands). Tune down only for a
  larger model that won't fit the full window.
- `OLLAMA_GPU_PASSTHROUGH` — leave blank for CPU; set non-blank
  for NVIDIA GPU passthrough via CDI.
- `OLLAMA_READINESS_TIMEOUT_SECONDS` — post-deploy model-pull
  deadline. Default `600`. Shared per-pull (not summed across the
  default + extras + vision); bump it if you pull large models on a
  slow link.

## CPU vs. GPU

| Mode | When | What's deployed |
|---|---|---|
| CPU (default) | No NVIDIA GPU, or you just want to kick the tyres | Plain Ollama pod, no `resources` block. Use small models (≤ 7B parameters) for acceptable latency. |
| GPU | NVIDIA GPU + CDI registered | Pod manifest gets `resources.limits.nvidia.com/gpu: "1"`. Podman matches this against the CDI device registry. Works with the larger models Ollama can run (gemma3:12b, qwen2.5:14b, llama3.1:70b on multi-GPU boxes). |

### Enabling GPU on the host (one-time)

```
sudo dnf install -y nvidia-container-toolkit   # or your distro's equivalent
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
podman info | grep -i cdi                       # confirm registration
```

Then set `OLLAMA_GPU_PASSTHROUGH=yes` in the ServiceBay wizard and
redeploy.

## Network exposure

Ollama ships **no built-in authentication**. The template binds
`OLLAMA_HOST=127.0.0.1:<port>` so only the host's loopback
interface accepts connections. Other ServiceBay pods that are
also `hostNetwork: true` (e.g. `hermes`) can reach it via that
loopback; bridge-networked pods cannot.

For LAN or remote access, put Ollama behind NPM + Authelia using
the forward-auth pattern (see `src/lib/stackInstall/forwardAuth.ts`
and the AdGuard / Syncthing admin pages for examples). **Do not**
flip `OLLAMA_HOST` to `0.0.0.0` and publish it directly — Ollama's
API exposes every loaded model to anyone who reaches it.

## What `post-deploy.py` does

1. Waits for `http://127.0.0.1:<port>/api/tags` to answer (the pod's
   own readiness signal).
2. POSTs `/api/pull` with `OLLAMA_DEFAULT_MODEL` so the first model
   is ready before the operator's first request.
3. If `OLLAMA_VISION_MODEL` is set, POSTs a second `/api/pull` for
   that model (sequential to keep network/disk pressure predictable).
4. Logs progress and emits a final "ready" line.

Idempotent — a second deploy with the same models finds them already
cached and skips the pulls.

## Multimodal (vision + audio) inference

The default `OLLAMA_DEFAULT_MODEL=gemma4:12b` is natively multimodal —
`ollama show gemma4:12b` reports `completion, vision, audio, tools,
thinking`. Solilos's `media-ingestion-multimodal` skill calls into it
without a separate vision pull.

Where the tag matters: the `OLLAMA_EXTRA_MODELS` default is empty, but
if you pre-pulled `VladimirGav/gemma4-26b-16GB-VRAM:latest`, that model
**drops vision and audio** in its 16 GB quant. An operator who switches Hermes' active
model to that tag for "smarter text reasoning" gives up multimodal
until they switch back to `gemma4:12b` (or pull plain `gemma4:26b`,
~17 GB with partial CPU offload, which keeps vision).

`OLLAMA_VISION_MODEL` is the explicit override for setups where the
default has been changed to a text-only tag. Suggested alternatives:

- `qwen2.5vl:7b` — Apache-2.0, ~6 GB quantised, fits a 16 GB GPU.
- `llava:13b` — older but well-tested, ~8 GB.
- `bakllava:7b` — Mistral-based LLaVA variant, ~5 GB.

Hermes' wiring picks up the new model automatically the next time
its config is regenerated — see `templates/hermes/README.md`.

## Thinking / reasoning output

`gemma4` and other reasoning-capable tags (`ollama show` reports a
`thinking` capability) emit their reasoning in `<thinking>…</thinking>`
blocks before the answer. This needs no Ollama-side parameter — it's a
native model capability, on by default. Solilos doesn't strip the
reasoning at the model layer (the chain-of-thought can improve answer
quality, and the gatekeeper/voice path TTS-reads only the final answer
anyway); instead the **chat panel's Settings → Chat → Thinking blocks**
toggle decides how a resident sees it: show the raw block, collapse it
to a `<details>` disclosure (default), or hide it. The preference is
per-browser and never round-trips to Ollama, so no template variable
gates it. If you switch to a non-reasoning tag, no `<thinking>` blocks
are produced and the toggle is simply a no-op.

## Storage

Models persist at `${DATA_DIR}/ollama/`. Pulled weights are large
(2–40 GB depending on model) — plan disk capacity accordingly.

## Health checks

A baseline `service`-type check (`Service: ollama`) is auto-created
by `ServiceManager.deployService`. The post-deploy.py script
additionally registers an HTTP check (`ollama-api`, 60 s) hitting
`http://127.0.0.1:<port>/api/tags` so degraded-but-running cases
(model corrupted, disk full, GPU OOM) surface as a `fail` instead
of going unnoticed.

See `docs/TEMPLATE_AUTHORING.md` § Health checks for the contract.

## Logging

Ollama's upstream image emits human-readable lines on stdout —
fine for `get_container_logs` / `get_podman_logs`. The post-deploy
script emits JSON-shaped lines per `docs/TEMPLATE_LOGGING.md` for
the events under its control (pull start, progress, ready).
