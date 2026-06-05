# OSCAR stack

End-to-end install for the household deployment of OSCAR on a
ServiceBay full-stack host.

Bundles: `ollama` + `hermes` + `hermes-chat` + `oscar-household`.
Does NOT bundle `home-assistant` or `voice` — those are smart-home
infra that lives in ServiceBay's default registry. Enable both
registries side-by-side if you want the full household setup.

## Services

ServiceBay's stack installer reads this checklist to learn which
templates the stack pulls in. All four are selected by default:

- [x] ollama
- [x] hermes
- [x] hermes-chat
- [x] oscar-household

For step-by-step installation instructions including registry setup,
operator UX, and post-install checks, see the [top-level
README](../../README.md).
