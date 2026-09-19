# llama.cpp (Household Model Server)

[llama.cpp](https://github.com/ggml-org/llama.cpp)'s `llama-server` serving
Solaris' models: **Gemma 4 E4B** for the household hot path — the voice turns,
the chat, the device commands, and (through the multimodal projector) the
photo and document descriptions — plus a second, small instance serving
**`nomic-embed-text`** for the vault's semantic search.

It replaced Ollama on the chat path in solarisbay#1318 and took over the last
two jobs — embeddings and the vision ingest — in solarisbay#1332. The `ollama`
template is retired; nothing on this box runs it any more.

## Router mode — four models, one port (solarisbay#1416)

Since #1416 llama-server runs as a **router**: one process, four model presets,
and the **client picks** with the `model` field of its request. `GET
/v1/models` lists what is on offer. The router listens on `LLAMA_ROUTER_PORT`
(loopback); what clients talk to on `LLAMA_PORT` is the **mode policy proxy**
in front of it (below).

| Preset | Model | Window | KV | Drafter | Vision | For |
|---|---|---|---|---|---|---|
| `gemma-4-e4b` | Gemma 4 E4B Q4_0 | 32 768 | f16 | MTP, n=4 | mmproj | the household — voice, chat, devices, photos |
| `gemma-4-12b` | Gemma 4 12B Q4_0 | 131 072 | q8 | MTP, n=4 | — | foundry evenings |
| `qwen3.6-35b-a3b` | Qwen 3.6 35B-A3B UD-IQ3_XXS | 131 072 | q8 | MTP, n=4 | — | reading and thinking |
| `qwen3.8-27b` | Qwen 3.8 27B UD-IQ3_XXS | 81 920 | q8 K / q4 V, `-ub 256` | MTP, **n=8** | — | coding |

The definitions live in `${DATA_DIR}/llama/models/presets.ini`, written by
post-deploy. The syntax is the one the box accepts and nothing else
(box-verified on image b10920, #1415): `long-option=value`, **no leading
dashes**, hyphens rather than underscores. Short flags, a whole command line
on one line and `ctx_size=` all fail, two of them with a message that does not
name the offending line. Every model option has to live there rather than in
the pod's argv: a command-line argument **outranks** a preset option, so one
`-c` on the router would silently give all four models the same window. What
does belong on the argv is what they share — the bind, `--jinja`, and
`--models-max 1`.

**One model is resident at a time.** The 16 GB card holds exactly one of
these, so `--models-max 1` lets the router evict the idle one and load the
asked-for preset in its own child process. Box-measured switching times: 9-19 s
warm, 15 s / 30 s / 51 s for the first cold load of e4b / 12B / Qwen. Two
requests for different presets at once do not crash it — the router serialises
(#1415). A release warms `gemma-4-e4b` again before it clears the lease, so
that wait does not land on the next resident.

**The router has no policy of its own.** It will load whatever it is asked
for. That is what the policy proxy in front of it is for.

## The mode policy proxy — what actually enforces the mode (solarisbay#1416)

`LLAMA_PORT` (11435) is held by **`solaris-llama-policy.service`**, a stdlib
process post-deploy installs; the router sits behind it on
`LLAMA_ROUTER_PORT` (11434), bound to loopback. Every client address is
unchanged — `LLAMA_SERVER_URL`, PI WEB's `models.json`, aider, goose all still
point at 11435 — and a normal turn is unchanged too, streams included.

What the proxy does, reading the lease file fresh on **every** request so a
mode taken from the phone is in force on the next one:

| Request | Answer |
|---|---|
| `model` in the mode's `allowed` set | forwarded to the router, streamed back chunk by chunk — and noted in the lease as the preset that is now being served (#1435) |
| `model` outside it | **409** `{"error": {"message": …, "mode": "haushalt", "allowed": ["gemma-4-e4b"]}}` |
| no `model` field | forwarded — the router answers from the preset it already has, which cannot be one outside the mode |
| `GET /v1/models` | the whole catalogue — every preset the router knows, each marked `allowed_in_mode`, with the standing `mode` at the top level |
| `/health`, `/props`, `/slots`, everything else | forwarded verbatim |

Why it exists: without it, one client asking for the 27B during a household
evening is *served*. `--models-max 1` then evicts Gemma, and the next resident
turn — a voice command, a light — waits 10-20 s for it to load again. The
Engine refusing that on its own side does not help, because the clients that
do it (PI WEB, aider, goose, Continue) never pass through the Engine. The
Engine's own check stays as well: it only ever asks for the preset the
standing mode names, so the refusal is belt and braces.

The message is German and says what to do (*"Für die anderen Modelle in der
Modell-Kachel in Solaris auf „Erweitert“ umschalten"*), because the operator is
who reads it — in PI WEB's
ticket protocol, in aider's error line, in a log someone scrolls.

### The listing names all four, the request is what is refused (#1431)

`/v1/models` used to be filtered down to the mode's `allowed` set, so a client
whose only door is this port saw exactly one model and could not learn that the
other three exist, what they are called, or that a mode switch reaches them.
Since #1431 it lists **every** preset and marks each one instead:

```json
{
  "object": "list",
  "mode": "haushalt",
  "data": [
    {"id": "gemma-4-e4b",     "status": {"value": "loaded"},   "allowed_in_mode": true},
    {"id": "gemma-4-12b",     "status": {"value": "unloaded"}, "allowed_in_mode": false},
    {"id": "qwen3.6-35b-a3b", "status": {"value": "unloaded"}, "allowed_in_mode": false},
    {"id": "qwen3.8-27b",     "status": {"value": "unloaded"}, "allowed_in_mode": false}
  ]
}
```

The marking is **additive**: `mode` and `allowed_in_mode` are added, nothing the
router sent is renamed or dropped, so a client that reads only `id` works
exactly as before.

**Two different axes, don't read one for the other.** `allowed_in_mode` is the
policy: may this client ask for the preset right now. The router's own
`status.value` (`loaded`/`unloaded`) is where the weights are: an *allowed*
preset is very often `unloaded`, which is normal and costs 7 to 17 s on the
first turn while it loads. A preset that is `loaded` but not allowed is equally
possible — the mode changed and the router has not evicted it yet.

Enforcement did not move: a request naming a preset outside the set still gets
the 409 above, with `mode` and `allowed`. The list informs; the request is what
is policed.

Two properties worth knowing:

* **SSE is not buffered.** The proxy reads the router one chunk at a time
  (`read1`, not `read` — the latter fills its buffer before returning and
  would hold a whole answer back until it is finished) and flushes each one.
* **It stays up during an exclusive lease**, when `llama.service` is stopped.
  Every model request is then refused with `allowed: []` and the sentence
  saying the card is handed out, rather than the connection simply failing.

`journalctl --user -u solaris-llama-policy.service` shows one line per refusal
and nothing per turn.

**Thinking is a per-request switch**, not a server flag: `"chat_template_kwargs":
{"enable_thinking": false}`, which the Engine sends on every household turn.
`--reasoning off` is gone from the coding preset with #1416 — one server now
serves four models and a server-wide switch would decide for all of them. Box
re-measured on #1415: the per-request switch works in router mode, the request
field `reasoning_budget: 0` does **not**. A client that sends neither (aider,
goose, Continue) gets a thinking trace and no tool call, so that setting is now
part of configuring the client.

## Why a second model server

Speculative decoding. Google publishes a Multi-Token-Prediction drafter for
Gemma 4, llama.cpp can use it, and **Ollama has no draft-model knob at all**.
Box-measured on the same weights, same prompt, same 28 generations
(solarisbay#1317/#1318):

| Server | tok/s | Seconds per finished answer |
|---|---|---|
| Ollama, `gemma4:e4b` | 53.0 | 0.62 s |
| llama-server + MTP drafter | 133.5 | **0.30 s** |

Tool calls were 12/12 on both, German answers complete on both.

## Configuration

- `LLAMA_PORT` — the port every client uses (default `11435`). What listens
  there is the **mode policy proxy**, not llama-server itself. No proxy route
  exists, so nothing here is reachable from the internet — but the home LAN
  reaches it without a login, on purpose. See *Who may reach the endpoint*
  below before you change `blockLanAccess`.
- `LLAMA_ROUTER_PORT` — the loopback port the router itself binds (default
  `11434`, Ollama's old port, free since that template was retired). Only
  post-deploy and `gpu-lease.py` talk to it.
- `LLAMA_MODEL_REPO` / `LLAMA_MODEL_FILE` / `LLAMA_DRAFT_FILE` /
  `LLAMA_MMPROJ_FILE` — what post-deploy downloads into
  `${DATA_DIR}/llama/models`. Defaults are ggml-org's Gemma 4 E4B Q4_0
  (4.59 GB), Google's MTP drafter (98.7 MB) and the vision projector
  (560 MB).
- `LLAMA_CONTEXT_LENGTH` — the window the **household** preset is loaded at
  (`32768`; the other three carry their own). Unlike Ollama this is fixed at
  load, not a per-request hint.
- `LLAMA_DRAFT_N_MAX` — drafted tokens per step for the household preset (`4`;
  43.4% accepted, against 25% at 8 — the coding preset runs 8, where Qwen's
  drafter is accepted 75% of the time).
- `LLAMA_GPU_PASSTHROUGH` — blank auto-detects a CDI-registered NVIDIA GPU.
- `LLAMA_EMBED_PORT` / `LLAMA_EMBED_REPO` / `LLAMA_EMBED_FILE` /
  `LLAMA_EMBED_ALIAS` / `LLAMA_EMBED_CONTEXT_LENGTH` — the embeddings server
  (below). Empty `LLAMA_EMBED_PORT` skips it.

Point the solaris template's `LLAMA_SERVER_URL` at
`http://127.0.0.1:<LLAMA_PORT>` and its `LLAMA_EMBED_URL` at
`http://127.0.0.1:<LLAMA_EMBED_PORT>`.

## The embeddings server (solarisbay#1332)

A second `llama-server`, its own Quadlet (`llama-embed.service`), loopback
only, ~300 MB of VRAM: `nomic-embed-text-v1.5` f16 with `--embeddings`,
serving OpenAI `/v1/embeddings` on `LLAMA_EMBED_PORT` (default `11436`). The
Solaris Engine embeds the OKF vector store and every semantic vault query
through it.

It is a separate unit rather than a second container in the pod because the
GPU fixup below replaces the pod's `.kube` unit outright, and a pod sibling
would go with it.

**Vector compatibility is the whole point of the defaults.** The rows already
in `okf_vectors` were computed by Ollama's `nomic-embed-text` tag: v1.5, f16,
768 dimensions, mean pooling, on the **raw text** — Ollama never applied the
`search_document:` / `search_query:` prefixes the model card describes, and
neither does Solaris. Same model, same quantisation, same pooling, same raw
text ⇒ old and new vectors are comparable and nothing had to be re-embedded.
Changing `LLAMA_EMBED_FILE` to a smaller quant, or adding a prefix, would not
fail — it would quietly make search worse.

Two flags in the unit are not tuning: `--pooling mean` (the model is
mean-pooled; anything else yields valid-looking, incomparable vectors) and
`--ubatch-size` equal to the context length (an embedding model runs
non-causal attention, and llama.cpp rejects anything longer than one
micro-batch).

## Who may reach the endpoint — an ADR-0007 carve-out, and one decided opening

There is no authentication anywhere here. Four consumers, four addresses:

| Consumer | Address | Why |
|---|---|---|
| Services on host networking — the Solaris Engine, the health check | `http://127.0.0.1:11435` | same netns; the default and the fast path |
| Isolated pods without host networking — pi-web, its `pi` | `http://host.containers.internal:11435` | ADR-0007 Decision 1: never `127.0.0.1`, never the LAN IP |
| Devices on the home LAN | `http://<box-lan-ip>:11435` | **allowed, deliberately** — see the decision below |
| post-deploy and `gpu-lease.py` | `http://127.0.0.1:11434` | the router direct: a release warms the household preset while a lease that forbids it still stands |

The **policy proxy** therefore binds `0.0.0.0`, not loopback (#1344). A
loopback bind looks like the safe choice and is not reachable from a sibling
pod at all: rootless podman/pasta maps `host.containers.internal`
(`169.254.1.2` here) to the host's **LAN address**, not to `127.0.0.1`, so
`pi`'s model picker came up empty against a loopback-bound server. Binding the
pasta-mapped address instead would hard-code a LAN IP and take `127.0.0.1`
away from the Engine — both forbidden.

The **router** binds `127.0.0.1:11434` and nothing else, which is the point:
it loads any preset it is asked for, so the proxy has to be the only thing
that can ask it.

### The LAN is open, and that is the decision — do not "fix" it

`LLAMA_PORT` carries **`blockLanAccess: false`** in `variables.json`. ServiceBay
builds its host nftables block set from exactly those declarations (`elements =
{ … }`, commented *ports a template declared blockLanAccess on*), so with the
flag off the port is never put in it and connections arriving on the physical
interface are not dropped.

**The operator asked for this** on 2026-09-13: *llama is to be available in the
local network, without a login, over the server's IP, and offer its models.* It
had been true on the box since before the ticket — but by a **hand-written**
`accept` rule in front of the drop, which is a state the next deploy can
overwrite and nobody can explain from the repository. #1420 moved the decision
into the contract so the reachability stops depending on a manual edit.

**What it costs, stated plainly.** llama-server ships no authentication and the
policy proxy adds none. Every device on the home network can therefore list the
models, send requests and spend GPU time on this box — a guest phone on the
Wi-Fi included. What it cannot do: evict the household model (the mode policy
refuses a preset the standing lease mode disallows, `409`), reach the router
directly (loopback-only), or get in from the internet (no proxy route, and
nothing forwards 11435 at the router). That trade was taken knowingly. Turning
the flag back on closes the LAN for everyone and is a decision to take **with
the operator**, not a tidy-up.

Before #1344 this port was loopback-bound, and the reason it binds `0.0.0.0`
today is the pod path, not the LAN one — the two arrived in that order, so a
reader finding the wide bind should not read it as the LAN decision.

Checking it on the box is four commands — from inside another pod
`curl http://host.containers.internal:11435/v1/models` must answer, from that
same pod `curl http://host.containers.internal:11434/v1/models` must **not**
(the router is loopback-only, so the policy cannot be walked around), from a LAN
host `curl http://<box-lan-ip>:11435/v1/models` must **answer**, and
`nft list set inet servicebay on_box_blocked` (or whatever the set is called on
the box) must no longer carry `11435`. Once it does not, the hand-written
`accept` rule in `on_box_gate` has nothing left to override and can go.

## Three traps, all box-measured

1. **`SecurityLabelDisable=true` is not optional.** With the CDI device but
   without the SELinux relaxation, llama-server logs one passing
   `no usable GPU found`, loads the model into RAM and answers from the CPU.
   Nothing anywhere reads as an error. The `.container` Quadlet post-deploy
   installs carries both lines; `podman kube play` drops the device
   altogether, which is why the fixup exists (#1026).
2. **`--draft-max` no longer exists.** The current image refuses to start on
   it ("the argument has been removed"). The MTP drafter needs
   `--spec-type draft-mtp --spec-draft-model … --spec-draft-ngl 99
   --spec-draft-n-max 4`, and `--spec-type` is mandatory. post-deploy checks
   `/slots` for `"speculative": true` after the start and warns when the
   drafter is not in play — otherwise the server just runs at half speed
   with no error.
3. **Thinking is on unless the request turns it off**, and since #1416 no
   server flag turns it off for everyone — one router serves four models.
   llama.cpp renders the
   chat template with `enable_thinking = true`, overriding the canonical
   Gemma template's own `default(false)`. The server flags
   (`--reasoning-budget 0`, `--reasoning-format none`) do *not* help — the
   second one makes it worse, dumping the raw reasoning trace into the
   visible answer. The switch is per request:
   `"chat_template_kwargs": {"enable_thinking": false}`, which the engine's
   `LlamaServerChat` sends on every household turn. With thinking on, the
   same 28 answers cost 4226 generated tokens instead of 674 and take 4.3x
   longer.

## The GPU lease — handing the whole card to another job

Several models want the one 16 GB card, and only Solaris' E4B (3.9 GB) is
always on. A job that needs more asks for it — at any hour, with no presence
check (solarisbay#1320).

**Since #1416 a lease no longer swaps the server.** The router already serves
every preset, so a named mode sets two things and nothing else:

1. **the environment** — whether the voice stack runs on the GPU or the CPU,
   and whether the background GPU jobs keep running;
2. **the mode policy** — the presets a client may ask for, written into the
   lease file as `allowed`.

**Two modes since #1435**, not four. What separated `foundry`, `thinking` and
`coding` was only the set of presets they allowed — the environment was the
same in all three, which is a distinction without a difference. The question a
mode has to answer is whether the card may serve something other than the
household; *which* model that then is, the client decides with the `model`
field, and the router loads it on demand.

| Mode | Environment | Allowed presets | Solaris answers from |
|---|---|---|---|
| `haushalt` (no lease) | voice GPU, batch jobs on, embeddings up | `gemma-4-e4b` | e4b |
| `--model erweitert` | voice **CPU**, batch jobs **off**, embeddings **down** | **every** preset the router knows | the preset that is loaded |
| no `--model` | everything stopped | none | nothing — the fixed sentence |

`foundry`, `thinking` and `coding` are still accepted as **aliases** of
`erweitert` (contract mdopp/foundry-chronicle#321), and decide only which
preset the holder is told it will be answered by. A lease file left on disk
under one of the old names migrates to `erweitert` when it is read. The preset
*definitions* are untouched — they describe weights, window and drafter and are
what `presets.ini` is rendered from.

A request for a preset the current mode does not allow is refused with the
mode's name rather than served: the household model is never evicted by a
stray request, and there is no thrashing between e4b and Qwen. **The policy
proxy on `LLAMA_PORT` is what refuses it** (above) — every client on the box
goes through it. The Engine checks on its own side too, asking only for the
preset the standing mode names; the router behind the proxy has no policy at
all.

post-deploy installs `${DATA_DIR}/solarisbay/gpu-lease.py` for that:

```
python3 ${DATA_DIR}/solarisbay/gpu-lease.py acquire someone
python3 ${DATA_DIR}/solarisbay/gpu-lease.py acquire coder --model erweitert --duration 4h
python3 ${DATA_DIR}/solarisbay/gpu-lease.py acquire foundry --model foundry --duration 5h
python3 ${DATA_DIR}/solarisbay/gpu-lease.py release
```

`acquire` writes `${DATA_DIR}/solarisbay/gpu_lease.json`; without `--model` it
then stops `llama-embed`, `solaris-whisper`, `solaris-whisper-batch`, `solaris-tts`,
`solaris-wakeword-trainer` and `llama` — the five units the night
measurements stopped by hand, plus Solaris' own model server. It refuses when
someone else already holds the lease. A named mode stops nothing on the llama
side — the router keeps serving. `release` starts whatever the mode stopped,
asks the router for a token from `gemma-4-e4b` so the household model is warm
again, and removes the lease file **last**.

**Every lease expires.** `--duration` defaults to 4 h and arms a transient
systemd timer (`solaris-gpu-lease-expiry`) that runs `release` at the
deadline. An end signal alone was not enough in solarisbay#1260: a run that
dies without releasing would otherwise leave the household muted, or the voice
stack on the CPU, until somebody noticed.

**What the resident gets meanwhile.** The lease file lands on the volume the
chat pod mounts, so the Engine sees it as `/var/lib/solaris/gpu_lease.json`
and answers every turn with one fixed German sentence — "Ich rechne gerade an
einer großen Aufgabe…" — instead of waiting out a timeout against a stopped
server. That is `solaris_chat/gpu_lease.py`; no request leaves the pod while
the lease is held. Voice is off for the duration: `solaris-whisper` and
`solaris-tts` are two of the stopped units.

### `--model coding` — the coding window (solarisbay#1319)

* Allowed preset: `qwen3.8-27b` — Qwen 3.8 27B `UD-IQ3_XXS` + its MTP drafter,
  `-c 81920 -ctk q8_0 -ctv q4_0 -ub 256 --parallel 1 --spec-draft-n-max 8`.
  `--parallel 1` and q8 keys are not tuning: with llama-server's stock four
  slots, or f16 KV, the drafter OOMs before it loads. The q4 **values** are new
  in #1415 — on image b10920 they cost 3% of prompt processing instead of the
  5-8x the older image charged, and the 640 MiB they free pay for the longer
  draft: 44.7 tok/s against 38.7, 12/12 tool calls, 15 652 of 16 380 MiB.
  The 12.6 GB of weights are fetched **before** anything stops.
* Solaris answers household turns from that model for the window (mode B) and
  the chat carries a banner naming the model and the end time.
* `solaris-whisper` and `solaris-tts` keep running, on the **CPU**: operator
  decision of 2026-09-05, spoken commands stay possible and get slower rather
  than disappearing. Both units read their provider from
  `${DATA_DIR}/solarisbay/voice-device.env`, which the lease flips to
  `cpu`/`cuda` and restarts them on; whisper drops to `small-int8` with it.
  `solaris-whisper-batch` and `solaris-wakeword-trainer` stop — they hold VRAM
  and nobody is waiting on them. **`llama-embed` keeps running** (operator,
  2026-09-13): its ~430 MiB fits under the 27B's 15 486 MiB peak — 15 923 of
  16 380 box-measured — and stopping it cost the household its semantic vault
  search for the whole window. The `thinking` window is the one that cannot
  afford it.

### `--model thinking` — reading and thinking (solarisbay#1416)

The operator's decision of 2026-09-13, measured on solarisbay#1418: long
documents and hard questions go to **Qwen 3.6 35B-A3B**, a mixture-of-experts
model with 3 of its 35 B parameters active per token.

* Allowed preset: `qwen3.6-35b-a3b` — `UD-IQ3_XXS` + the official ggml-org MTP
  drafter, `-c 131072 -ctk q8_0 -ctv q8_0 --parallel 1`. Box-measured 15 620 of
  16 380 MiB, **105 tok/s** (the 27B does 39), 866 tok/s prefill, 83.5% drafter
  acceptance, 12/12 tool calls, and it found a planted sentence in an
  85 287-token prompt. Only 10 of its 40 layers carry KV, which is why 131k
  costs just 792 MiB more than 82k.
* Same environment as the coding window: voice on the **CPU**,
  `solaris-whisper-batch` and `solaris-wakeword-trainer` stopped. At 15 620 of
  16 380 MiB there is no room for the voice stack on the card — and, unlike the
  coding window, none for the embeddings server either: the box measured the
  MTP drafter's compute buffer failing to allocate by 168 MiB with
  `llama-embed` resident, which makes the whole preset fail to load and the
  mode serve nothing. **`llama-embed` therefore stops for this mode alone** and
  starts again on release, so the vault loses its semantic search for the
  window. This is the one mode where it does.
* No vision projector. The mmproj exists (614 MB, ggml-org) but vision was
  only measured to 98k, and the operator scoped this preset to the 131k text
  window. A photo reaches it as text.

### `--model foundry` — the foundry evening (solarisbay#1325)

foundry writes up a session as it runs and transcribes through
`solaris-whisper-batch` every five minutes, so the one thing it must not do is
take the voice stack away. This mode therefore stops **nothing**:

* Allowed presets: `gemma-4-12b` **and** `gemma-4-e4b` — foundry is the one
  mode where the household keeps its own model on the menu. The 12B runs
  `-c 131072 -ctk q8_0 -ctv q8_0` since #1415: 10 156 MiB, 520 MiB more than
  the 32k f16 cell of #1318, and it carried an 85k prompt at 686 tok/s with
  12/12 tool calls. Only one of the two is resident at a time, so the household
  turn and a foundry turn trade the card at 9-19 s a switch.
* All five units — `llama-embed`, `solaris-whisper`, `solaris-whisper-batch`,
  `solaris-tts`, `solaris-wakeword-trainer` — keep running, on the **GPU**;
  `voice-device.env` is not touched. The 12B leaves 6 GB spare, so nothing has
  to move at all.
* Solaris answers the household from the 12B and **shows no banner**: operator
  decision of 2026-09-05. Nothing the resident does changes — voice included —
  except that an answer takes about a second longer. `/api/whoami` still names
  the window under `gpu_lease` for the log.
* No vision projector: the 12B repo's `mmproj` has never been fetched or
  measured on this box. A photo attachment reaches the 12B as text for the
  window.

A deploy in the middle of a lease leaves `llama.service` and the voice device
exactly as the lease set them; post-deploy says so in its log and does nothing
else. Restarting the router would drop the preset the holder has loaded and
charge it the cold load again, mid-run.

The lease file's *presence* is the whole signal, deliberately: it is written
before anything stops and removed after the household model is warm again, so
there is no window where the card is gone and nothing knows it. Its `allowed`
list is the mode policy the Engine enforces.

The `llama-api` health check goes red for the duration of an exclusive lease —
expected, and the one thing to watch on the box: nothing may restart
`llama.service` behind the lease's back. A named mode keeps the check green;
it is the same router either way.

## Storage

`${DATA_DIR}/llama/models` — about 40 GB: 5.2 GB of household weights, 7.7 GB
of 12B, 12.6 GB of Qwen 27B and 14.3 GB of Qwen 35B-A3B, plus `presets.ini`.
All four presets are fetched at install rather than at the first lease, because
the router lists every one of them from its first start. post-deploy
downloads to `<name>.part` and renames on completion, so an interrupted
download never leaves a truncated GGUF that llama-server would crash-loop on.

## Health checks

`/health` on 11435 goes through the policy proxy to the router, so one probe
covers the whole chain a consumer uses — a dead proxy reads as a dead service,
which is what it is. That is the liveness signal. Readiness of a *model* is a
different question, so post-deploy asks the household preset for one token
after the install (against the router direct) and warns if it does not come.
post-deploy registers 11435 as the `llama-api` HTTP check (60 s) on top of the
auto-created `service`-type check.
