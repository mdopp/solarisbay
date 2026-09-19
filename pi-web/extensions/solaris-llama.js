/**
 * `solaris-llama` as a NATIVE Pi provider, so its model list refreshes itself (#1435).
 *
 * The problem this removes: Pi's model list for our provider was the static
 * `models` array in `/data/pi-agent/models.json`, a file on the data volume
 * written once per deploy. It listed three of the four presets and was dated
 * before #1431 made all four visible, which is why `gemma-4-12b` could not be
 * picked in PI WEB at all. Nothing on the box would ever have corrected it.
 *
 * Pi does run a background catalog refresh — 15 s after the session daemon
 * starts, then hourly — but it had nothing to call for us. In
 * `@earendil-works/pi-ai/dist/models.js`, `createProvider` sets
 * `refreshModels: fetchModels ? … : undefined`, and a provider that arrives in
 * CONFIG form (`pi.registerProvider("id", config)`, which is what a models.json
 * entry becomes) brings no `fetchModels`. So the schedule ran and skipped us.
 *
 * A provider registered in NATIVE form (`pi.registerProvider(provider)`) may
 * carry its own `refreshModels`, and PI WEB captures that form explicitly at
 * daemon start (`@jmfederico/pi-web/docs/config.md`, "Pi extension provider
 * baseline"). That is all this file is: the same provider, registered with a
 * fetch that asks the pod's model gate. After it, Pi keeps the list current by
 * itself — no script, no systemd timer, no recurring restart.
 *
 * **One restart, not a recurring one.** Providers are captured at daemon start
 * and frozen for its lifetime, so installing or changing this extension needs
 * `systemctl --user restart pi-web-sessiond` once. The pod's `pi-web-extensions`
 * init container puts the file in place before `sessiond` starts, so an ordinary
 * deploy already is that restart. Later MODEL LIST changes need nothing: the
 * background refresh reaches into this provider's own closure.
 *
 * **The names come from the gate, deliberately.** Once a fetch exists, pi-ai
 * merges by id and a fetched entry REPLACES the hand-written one — display name,
 * `contextWindow`, `maxTokens` and `chatTemplateKwargs` included. So the gate
 * serves them (`pi_model_gate.py`, `PRESETS`) and this file only maps them. The
 * table then lives in one place that ships with the pod instead of in a file on
 * a volume that can go stale.
 */

import { stream, streamSimple } from "@earendil-works/pi-ai/compat";

const PROVIDER_ID = "solaris-llama";
const PROVIDER_NAME = "Solaris llama-server";
const DEFAULT_GATE_URL = "http://127.0.0.1:11437/v1";

// llama-server ships no authentication, so there is no key to hold — but Pi
// hides a model whose provider has no auth configured at all, so the provider
// carries a placeholder, exactly as upstream's own Ollama example does.
const API_KEY = "llama";

const FETCH_TIMEOUT_MS = 15_000;

function gateUrl() {
  return (process.env.PI_MODEL_GATE_URL || DEFAULT_GATE_URL).replace(/\/+$/u, "");
}

function positive(value, fallback) {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? Math.floor(value) : fallback;
}

function toModel(entry) {
  const pi = entry.pi;
  const input = Array.isArray(pi.input) && pi.input.length > 0 ? pi.input : ["text"];
  return {
    id: entry.id,
    name: typeof pi.name === "string" && pi.name ? pi.name : entry.id,
    api: "openai-completions",
    provider: PROVIDER_ID,
    baseUrl: gateUrl(),
    reasoning: pi.reasoning === true,
    input,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: positive(pi.contextWindow, 32768),
    maxTokens: positive(pi.maxTokens, 16384),
    compat: typeof pi.compat === "object" && pi.compat !== null ? pi.compat : {},
  };
}

async function fetchCatalog(signal) {
  const timeout = AbortSignal.timeout(FETCH_TIMEOUT_MS);
  const response = await fetch(`${gateUrl()}/models`, {
    signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
    headers: { Authorization: `Bearer ${API_KEY}` },
  });
  if (!response.ok) throw new Error(`Model gate answered HTTP ${response.status}`);
  const payload = await response.json();
  const data = payload && Array.isArray(payload.data) ? payload.data : undefined;
  if (!data) throw new Error("Model gate answered no catalog");
  return data
    .filter((entry) => entry && typeof entry.id === "string" && typeof entry.pi === "object" && entry.pi !== null)
    .map(toModel);
}

export default async function solarisLlamaExtension(pi) {
  // The first catalog is awaited so the picker is populated from the very first
  // session rather than from the first background refresh 15 s later. PI WEB
  // awaits an async extension factory during its provider bootstrap, so this
  // still joins the frozen baseline. A gate that is not listening yet is not an
  // error — the provider registers empty and the background refresh fills it.
  let models = [];
  try {
    models = await fetchCatalog();
  } catch (error) {
    console.warn(`solaris-llama: starting with an empty catalog (${error instanceof Error ? error.message : error})`);
  }

  pi.registerProvider({
    id: PROVIDER_ID,
    name: PROVIDER_NAME,
    baseUrl: gateUrl(),
    auth: {
      apiKey: {
        name: PROVIDER_NAME,
        check: async () => ({ type: "api_key", source: "model gate" }),
        resolve: async () => ({ auth: { apiKey: API_KEY, baseUrl: gateUrl() }, source: "model gate" }),
      },
    },
    getModels: () => models,
    refreshModels: async (context) => {
      if (context.stored) {
        const restored = context.stored.models.filter((model) => model.provider === PROVIDER_ID);
        if (!(await context.publish({ update: () => { models = restored; } }))) return;
      }
      if (!context.allowNetwork || context.signal.aborted) return;
      const refreshed = await fetchCatalog(context.signal);
      if (context.signal.aborted) return;
      await context.publish({
        persist: { models: refreshed, checkedAt: Date.now() },
        update: () => { models = refreshed; },
      });
    },
    stream: (model, context, options) => stream(model, context, options),
    streamSimple: (model, context, options) => streamSimple(model, context, options),
  });
}
