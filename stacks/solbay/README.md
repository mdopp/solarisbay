# Solilos stack

End-to-end install for the household deployment of Solilos on a
ServiceBay full-stack host.

Bundles: `ollama` + `solilos`. The merged `solilos` service (#271) is one
ServiceBay service / one tile holding the Hermes runtime, chat UI,
household glue + skills, voice bridge, and operator soul as separate
containers in one Pod; `ollama` stays its own service (GPU/LLM engine).
Does NOT bundle `home-assistant` or `voice` — those are smart-home
infra that lives in ServiceBay's default registry. Enable both
registries side-by-side if you want the full household setup.

## Services

ServiceBay's stack installer reads `stack.yml`'s `templates:` list (this
checklist mirrors it). Both are selected by default:

- [x] ollama
- [x] solilos

For step-by-step installation instructions including registry setup,
operator UX, and post-install checks, see the [top-level
README](../../README.md).
