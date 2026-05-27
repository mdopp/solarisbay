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
  points at after install. Default `gemma4:e4b` — 8B params @ Q4_K_M,
  ~10 GB on disk, fits 100% on a 16 GB GPU, AND ships native multimodal
  (`ollama show gemma4:e4b` reports `completion, vision, audio, tools,
  thinking`). That single default backs OSCAR's multimodal-ingestion
  path without a separate vision pull. Any Ollama library tag works,
  plus user-namespaced tags like `VladimirGav/gemma4-26b-16GB-VRAM:latest`.
- `OLLAMA_EXTRA_MODELS` — comma-separated list of additional models
  pre-pulled at install time on top of the default. Gives the operator
  one-click switchable choices in Hermes' Models tab without a fresh
  download. Default ships `VladimirGav/gemma4-26b-16GB-VRAM:latest` —
  a quantized 26B that still fits 100% on a 16 GB GPU, complementing
  the smaller default for harder text reasoning. **Caveat:** the 16
  GB quant strips vision and audio from its `Capabilities` block — it's
  text-only. Switching the active model to it drops multimodal; switch
  back to `gemma4:e4b` (or pull plain `gemma4:26b` ~17 GB with partial
  CPU offload) for OCR / voice-note flows. Each extra adds 10–20
  minutes to the install on a typical home link. Set to empty string
  to skip.
- `OLLAMA_VISION_MODEL` — historical, mostly unused now that the
  default `gemma4:e4b` ships vision + audio natively. Set this only if
  you've changed `OLLAMA_DEFAULT_MODEL` to a text-only tag and still
  want a vision backend for OSCAR's `media-ingestion-multimodal` skill.
  Suggested non-default tags: `qwen2.5vl:7b`, `llava:13b`, `bakllava:7b`.
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

The default `OLLAMA_DEFAULT_MODEL=gemma4:e4b` is natively multimodal —
`ollama show gemma4:e4b` reports `completion, vision, audio, tools,
thinking`. OSCAR's `media-ingestion-multimodal` skill calls into it
without a separate vision pull.

Where the tag matters: the `OLLAMA_EXTRA_MODELS` default ships
`VladimirGav/gemma4-26b-16GB-VRAM:latest`, which **drops vision and
audio** in its 16 GB quant. An operator who switches Hermes' active
model to that tag for "smarter text reasoning" gives up multimodal
until they switch back to `gemma4:e4b` (or pull plain `gemma4:26b`,
~17 GB with partial CPU offload, which keeps vision).

`OLLAMA_VISION_MODEL` is the explicit override for setups where the
default has been changed to a text-only tag. Suggested alternatives:

- `qwen2.5vl:7b` — Apache-2.0, ~6 GB quantised, fits a 16 GB GPU.
- `llava:13b` — older but well-tested, ~8 GB.
- `bakllava:7b` — Mistral-based LLaVA variant, ~5 GB.

Hermes' wiring picks up the new model automatically the next time
its config is regenerated — see `templates/hermes/README.md`.

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
