# Changelog

## [0.10.0](https://github.com/mdopp/solbay/compare/v0.9.0...v0.10.0) (2026-06-12)


### Features

* **chat:** add the Sol Engine core replacing the Hermes gateways ([3542115](https://github.com/mdopp/solbay/commit/35421153b8b93559d4c004e51f0a3df585520596))
* **chat:** complete the Sol Engine — facade, crons, admin MCP, Hermes retired ([34da56f](https://github.com/mdopp/solbay/commit/34da56f8f0a3e9eea1cf368e1d7fdd51fa4c15cb))
* **chat:** decouple everyday-chat model preference from persona via settings toggle ([eb711e3](https://github.com/mdopp/solbay/commit/eb711e3a255e38e0ac27038d5e895812e79872f0))
* **chat:** move the soul to the chat-owned volume with direct panel writes ([cebeca8](https://github.com/mdopp/solbay/commit/cebeca84c384cc5c5631fd4e3024988d7a0708fd))
* **db:** add engine cron run stamps table ([33a8ab4](https://github.com/mdopp/solbay/commit/33a8ab4dd176b534d9406d6fa5cef2bef7633f9a))
* **db:** add engine session, message and timer tables ([08925df](https://github.com/mdopp/solbay/commit/08925df14556a795044961944cf96108ed23b807))
* **gatekeeper:** speak the engine facade instead of Hermes sessions ([79fd562](https://github.com/mdopp/solbay/commit/79fd5629a7ab679fb7d57bbb07efc553c4ee8be1))
* Sol Engine Phase 0+1 — native agent core replaces the Hermes gateways ([e492e1c](https://github.com/mdopp/solbay/commit/e492e1c0573cea35b6829bc5e032ea0e0562194c))
* **template:** engine-only solilos pod with HA voice-pipeline wiring ([cfa4eb0](https://github.com/mdopp/solbay/commit/cfa4eb0a3a6fbc7af2cb15d046e4dc65af186f28))
* **template:** keep all three models resident with a right-sized 32k context ([9ecd96d](https://github.com/mdopp/solbay/commit/9ecd96d65e972c736d89e75b79e45c44b3b7e02f))
* **template:** wire the chat container for the Sol Engine ([08bd478](https://github.com/mdopp/solbay/commit/08bd478c20907a0adf866679a79f5226d73401cd))


### Bug Fixes

* **chat:** target every assist satellite when a timer announces ([d25643c](https://github.com/mdopp/solbay/commit/d25643c4e3ecfa89a0c7be1122774dd4aca78189))
* **template:** align the GPU render-path defaults with the v7 residency values ([a8364b5](https://github.com/mdopp/solbay/commit/a8364b51aae8831042e2bd5730acfff4d82b67b4))
* **template:** box-verified voice-wiring fixes and an abort-path guard ([f303bbe](https://github.com/mdopp/solbay/commit/f303bbefd9a699c6d2047f5486701814d8f2f223))
* **template:** pin the pipeline to the wyoming engines and both PE selects ([4279b65](https://github.com/mdopp/solbay/commit/4279b65e55383d71e6656574942d4554c44b9939))
* **template:** retry async HA setup races in the voice wiring ([1459462](https://github.com/mdopp/solbay/commit/14594621aea1e95944a2c12979eaa7dc217beffb))

## [0.9.0](https://github.com/mdopp/solbay/compare/v0.8.1...v0.9.0) (2026-06-10)


### Features

* **chat:** route the Sol Gründlich persona to the sol-deep gateway ([ec6ab02](https://github.com/mdopp/solbay/commit/ec6ab021e47b59f4bf619f797c514f167e73b28e))
* Sol Gründlich — the Sol identity on 12b for thorough chat + crons ([8aa3b61](https://github.com/mdopp/solbay/commit/8aa3b612f383e5e0dbff4d375b126b3c4cad0d0a))
* **template:** provision a sol-deep Hermes profile on 12b for the Gründlich mode and crons ([0039880](https://github.com/mdopp/solbay/commit/00398808c8532a3dc00e51148a6fe146637019d3))

## [0.8.1](https://github.com/mdopp/solbay/compare/v0.8.0...v0.8.1) (2026-06-10)


### Bug Fixes

* chat turns ≤2s — stop fast-model thinking + keep chat/embed models resident ([1e750a7](https://github.com/mdopp/solbay/commit/1e750a7f580da77c621235bc12b3df4a75330635))
* **chat:** stop the fast model thinking on every turn so chat turns are sub-2s ([19b03be](https://github.com/mdopp/solbay/commit/19b03bed0110cd7fc2f9ebc4ca330fd16fb5c695))
* **template:** drop session_search + todo from the household toolset prefill ([1f1d9c6](https://github.com/mdopp/solbay/commit/1f1d9c6b80b1e2237dbda4bc2d565b419635ab18))
* **template:** drop session_search + todo from the household toolset prefill ([bd2abb4](https://github.com/mdopp/solbay/commit/bd2abb4a604a65cc79068290d4a68e34ac1fc9ac))
* **template:** keep chat + embed models resident with OLLAMA_MAX_LOADED_MODELS=2 ([2b0a8a2](https://github.com/mdopp/solbay/commit/2b0a8a2d6f5f4931c53014a0cc6a5f646286f8d1))
* **template:** remove already-seeded bundled skills from the household home ([a4d1f76](https://github.com/mdopp/solbay/commit/a4d1f76976c264d2cc9fcccd203241264e03c378))
* **template:** remove already-seeded bundled skills from the household home ([a7cf447](https://github.com/mdopp/solbay/commit/a7cf44780ed8491e2a77aabe1959ecf82c9c97e3))

## [0.8.0](https://github.com/mdopp/solbay/compare/v0.7.0...v0.8.0) (2026-06-10)


### Features

* **chat:** tag each LLM trace step with its Hermes profile ([e8021fd](https://github.com/mdopp/solbay/commit/e8021fda34cf8dbab1b22c050bf2dfe703862242))
* **chat:** tag each LLM trace step with its Hermes profile ([c0956d5](https://github.com/mdopp/solbay/commit/c0956d555416a5c09b3f4b1b595bdcca9512d6e8))


### Bug Fixes

* **chat:** keepalive heartbeat so tool-turn answers survive the long Ollama prefill ([51b5419](https://github.com/mdopp/solbay/commit/51b54195585186fa9880b02f2846df032e1340c9))
* **chat:** keepalive the SSE stream through the tool round-trip so the answer renders ([7150559](https://github.com/mdopp/solbay/commit/7150559847af013de9a0ac0cb5b80f116aa13854)), closes [#319](https://github.com/mdopp/solbay/issues/319)
* **template:** make install_gpu_quadlet_fallback activation-idempotent ([761a0fd](https://github.com/mdopp/solbay/commit/761a0fd6efac3e48cdcd43221378cb2708450bb4)), closes [#322](https://github.com/mdopp/solbay/issues/322)
* **template:** make ollama GPU-quadlet fallback activation-idempotent so redeploys keep the GPU ([8eff436](https://github.com/mdopp/solbay/commit/8eff4360e38c1bd6fb860eb827796189ac5a214d))

## [0.7.0](https://github.com/mdopp/solbay/compare/v0.6.0...v0.7.0) (2026-06-09)


### Features

* always-on Ollama trace proxy — permanent LLM traceability (phase 1) ([a7904d6](https://github.com/mdopp/solbay/commit/a7904d6e9cf328a8ad9910144a3d87f2140c125c))
* **chat:** hide internal hint prefixes in history + Wiederholen re-run ([7737c61](https://github.com/mdopp/solbay/commit/7737c61981c0a0bcceb76a6e5df3fbab1aa9efc6)), closes [#309](https://github.com/mdopp/solbay/issues/309) [#308](https://github.com/mdopp/solbay/issues/308)
* **gatekeeper:** trim MCP prefill noise — suppress empty FastMCP capabilities and drop gatekeeper-mcp from the household profile ([f129284](https://github.com/mdopp/solbay/commit/f129284bb63a889e840944b6639f2338e51b0c48)), closes [#312](https://github.com/mdopp/solbay/issues/312) [#313](https://github.com/mdopp/solbay/issues/313)
* household runtime batch — SOUL HA grounding, prefill curation, trace detail endpoint ([1c37e52](https://github.com/mdopp/solbay/commit/1c37e524af87d37e5f756413e7dd99362c4613a1))
* **template:** always-on Ollama trace proxy for permanent LLM traceability ([2098390](https://github.com/mdopp/solbay/commit/20983908e0d2bb61dd25196d397d812e59b930e5))
* **template:** curate household default profile — drop servicebay-mcp + bundled skills from the first-turn prefill ([c42f691](https://github.com/mdopp/solbay/commit/c42f69167f7e3356af20cd99e98078dbf24a0f64)), closes [#292](https://github.com/mdopp/solbay/issues/292)
* **template:** hide internal hint prefixes in chat history + add Wiederholen re-run ([dd64ec3](https://github.com/mdopp/solbay/commit/dd64ec3ccd389d494113243d1d4e8238f00fbdb9))
* **template:** per-turn LLM-step trace panel in the chat UI ([6054cca](https://github.com/mdopp/solbay/commit/6054cca56a493207cbaa8cb4f128a125b1c820e8))
* **template:** per-turn LLM-step trace panel in the chat UI ([675a554](https://github.com/mdopp/solbay/commit/675a554e9095ca022ce955aea28f3d91326162d4)), closes [#307](https://github.com/mdopp/solbay/issues/307)
* **template:** persist per-message LLM trace and serve it reopen-consistently ([0fc6786](https://github.com/mdopp/solbay/commit/0fc678697e8ea8ec523c3563fdf2dd4cdf6348b8)), closes [#306](https://github.com/mdopp/solbay/issues/306)
* **template:** serve exact per-call trace content at /__traces__/&lt;id&gt; ([58797c8](https://github.com/mdopp/solbay/commit/58797c829e1018e7bcf6099bd32876a60fd5c739)), closes [#305](https://github.com/mdopp/solbay/issues/305)
* **template:** trace persistence, SOUL.md bind-mount, household MCP trim ([f933103](https://github.com/mdopp/solbay/commit/f9331033d5997878266a213bbfd2b48da8852103))


### Bug Fixes

* **chat:** retry session create on a title collision instead of (no reply) ([b45a595](https://github.com/mdopp/solbay/commit/b45a5957ca32489b256020519157d5c798e25f7a))
* **chat:** retry session create on title collision — household (no reply) ([#301](https://github.com/mdopp/solbay/issues/301)) ([bb44b0b](https://github.com/mdopp/solbay/commit/bb44b0bf0f7a560735603a8827f73cc2e72d3b22))
* **ci:** make release-please reliably trigger the image build for the tag ([7979a5f](https://github.com/mdopp/solbay/commit/7979a5fabed2ee6a44d83898bf76e0c236c736bc))
* **ci:** make release-please reliably trigger the tag image build ([ccc334b](https://github.com/mdopp/solbay/commit/ccc334b4769910f959cf5a08ac5f6bd18865c453))
* **template:** keep the admin gateway up across reboots ([d70b18b](https://github.com/mdopp/solbay/commit/d70b18b60885a798260294c46b5a54af14e08432))
* **template:** keep the admin gateway up across reboots ([#299](https://github.com/mdopp/solbay/issues/299)) ([c7f36e2](https://github.com/mdopp/solbay/commit/c7f36e279392bf5752091fb42b11a59504dd1c34))
* **template:** point Hermes at the trace proxy permanently via the container env ([b28315a](https://github.com/mdopp/solbay/commit/b28315aeb33bffd51ef005a86c08588cde67f175))
* **template:** read HA states entity-by-entity so Sol stops reporting all-off ([fea9413](https://github.com/mdopp/solbay/commit/fea9413e45ae9103283ef086bd90a74e51adc3fe)), closes [#289](https://github.com/mdopp/solbay/issues/289)
* **template:** render trace step-detail from nested request/response shape ([fe3476f](https://github.com/mdopp/solbay/commit/fe3476f08a8040473f6a96e8030f6a71fb50afbb))
* **template:** renderTraceDetail reads nested request/response trace shape ([69fdea7](https://github.com/mdopp/solbay/commit/69fdea7940549aacbb8c572acb3c6775694962f3)), closes [#316](https://github.com/mdopp/solbay/issues/316)
* **template:** route Hermes through the trace proxy permanently (read provider from container env) ([2461ee0](https://github.com/mdopp/solbay/commit/2461ee0ddda86b00fa0d8e9f0cdd6a1f18beb15a))
* **template:** ship SOUL.md via the container bind-mount so post-deploy actually installs it ([f10a906](https://github.com/mdopp/solbay/commit/f10a9065bb9bb8b72e228f924045acb3fa3a0d1b)), closes [#311](https://github.com/mdopp/solbay/issues/311)

## [0.6.0](https://github.com/mdopp/solbay/compare/v0.5.0...v0.6.0) (2026-06-09)


### Features

* **solilos-chat:** route chat turns to the household or admin Hermes gateway ([c4872c7](https://github.com/mdopp/solbay/commit/c4872c77f543613d518a04e4478c97d5c759e6f3)), closes [#293](https://github.com/mdopp/solbay/issues/293)
* **template:** instance-per-profile Hermes — household + admin gateway containers ([75adeb4](https://github.com/mdopp/solbay/commit/75adeb4b3df449ac3e877dd401ce4b722fac9982)), closes [#293](https://github.com/mdopp/solbay/issues/293)
* **template:** multi-profile Hermes — household + admin gateways per profile ([2d83c16](https://github.com/mdopp/solbay/commit/2d83c162977d6c8cc0af914ce1a28eec43ec8a0b))
* **template:** multi-profile Hermes — household + isolated admin gateway in one container ([#293](https://github.com/mdopp/solbay/issues/293)) ([f30dead](https://github.com/mdopp/solbay/commit/f30dead91f0f501c9013387f088f08ac86738a47))
* **template:** multi-profile Hermes via one container, household=default + admin secondary ([b03297f](https://github.com/mdopp/solbay/commit/b03297f690201b423e3735089705a2eb61ce3e99))
* **template:** pin voice gatekeeper to the household Hermes gateway ([2159088](https://github.com/mdopp/solbay/commit/2159088ba4b7cc120570cbd6970727504dc3aa66)), closes [#293](https://github.com/mdopp/solbay/issues/293)
* **template:** provision household + admin Hermes profiles in post-deploy ([c40276e](https://github.com/mdopp/solbay/commit/c40276ee355fa581d77a9460e0ba0bc3dd13e1b4)), closes [#293](https://github.com/mdopp/solbay/issues/293)

## [0.5.0](https://github.com/mdopp/solbay/compare/v0.4.1...v0.5.0) (2026-06-08)


### Features

* **chat:** combine persona+speed dropdown, left-align search, name Zuhause chat ([4c73411](https://github.com/mdopp/solbay/commit/4c734119a32dd5f08ae7ab3cb0910c65b87ec5ac)), closes [#278](https://github.com/mdopp/solbay/issues/278) [#280](https://github.com/mdopp/solbay/issues/280) [#281](https://github.com/mdopp/solbay/issues/281)
* **chat:** declutter header — Thinking toggle + context-fixed selectors ([4778935](https://github.com/mdopp/solbay/commit/47789359332d96d6c59a4e4a0ad93a0bcc24ebee))
* **chat:** declutter header — Thinking toggle + context-fixed selectors ([0b5d330](https://github.com/mdopp/solbay/commit/0b5d33036a13369bc1dfa34bc233a2bfee149990)), closes [#274](https://github.com/mdopp/solbay/issues/274)
* **chat:** household first-turn reply + header dropdown/title + SOUL HA grounding ([fa8a76e](https://github.com/mdopp/solbay/commit/fa8a76e55dd1bcd5a321db5880d13f275fc80c2e))
* **chat:** inline #tag/[@person](https://github.com/person) multitag — mentions backend, autosuggest, tag-cloud, retire Thema picker ([ca02fac](https://github.com/mdopp/solbay/commit/ca02facdd109e6d0a4b4defd4a75bb5ff1c5663b))
* **chat:** mention autosuggest popover + sent-turn highlight (279b) ([637306f](https://github.com/mdopp/solbay/commit/637306f423ace720e9cac696e2375f764b2f2e7e)), closes [#279](https://github.com/mdopp/solbay/issues/279)
* **chat:** mentions backend for inline #tag/[@person](https://github.com/person) (279a) ([94bee55](https://github.com/mdopp/solbay/commit/94bee551409e2dc17b5909c370609765517a0d14)), closes [#279](https://github.com/mdopp/solbay/issues/279)
* **chat:** responsive tag-cloud + jump-to-message (279c) ([d006c4d](https://github.com/mdopp/solbay/commit/d006c4dfe3d31483ed3fdabb9a6f721f5824fcb4)), closes [#279](https://github.com/mdopp/solbay/issues/279)
* **chat:** retire user-facing Thema topic picker (279d) ([98e610d](https://github.com/mdopp/solbay/commit/98e610d544c92b7dbccab8e207f4bff1b026db32)), closes [#279](https://github.com/mdopp/solbay/issues/279)
* **template:** merge hermes/chat/solbay/admin-soul into one solilos service ([6fe0d11](https://github.com/mdopp/solbay/commit/6fe0d114d525c1728b70467ef080b0c2a26575f6))
* **template:** merge hermes/chat/solbay/admin-soul into one solilos service ([dab508e](https://github.com/mdopp/solbay/commit/dab508e014b830adf32019c159b81fe3a8407d26)), closes [#271](https://github.com/mdopp/solbay/issues/271)
* **template:** TTFT prefill trim - drop household admin MCP, ollama anti-eviction, disable kanban ([867ab8e](https://github.com/mdopp/solbay/commit/867ab8ee325b9b3e4b53b2ed7505f5b7459c667c))
* **template:** TTFT trim — drop household servicebay_admin MCP, ollama anti-eviction, disable kanban ([8131fa7](https://github.com/mdopp/solbay/commit/8131fa72fedd4337606367001929fa81f61bbb84)), closes [#268](https://github.com/mdopp/solbay/issues/268)


### Bug Fixes

* **chat:** give first-turn session a unique title to avoid bare-marker collision ([87f5b78](https://github.com/mdopp/solbay/commit/87f5b783e3d37f574fecbffe19f47499b0967204)), closes [#277](https://github.com/mdopp/solbay/issues/277)
* **chat:** unique ephemeral [temp:] title so a 2nd incognito chat can't 400 ([7c479fe](https://github.com/mdopp/solbay/commit/7c479fefe57c7b18f9266fba387fc7cfc0f993ae))
* **chat:** unique ephemeral [temp:] title so a 2nd incognito chat can't 400 ([c403381](https://github.com/mdopp/solbay/commit/c4033817d949a68ebdc58347e14794720082fc6c)), closes [#286](https://github.com/mdopp/solbay/issues/286)
* **template:** land shipped SOUL.md changes on existing installs via a shipped-hash sidecar ([9dcbd79](https://github.com/mdopp/solbay/commit/9dcbd790220d4d1f2f61625c79ddf5eaf1fb6984))
* **template:** land shipped SOUL.md changes on existing installs via a shipped-hash sidecar ([7e0da6d](https://github.com/mdopp/solbay/commit/7e0da6d5d8ed368e2c82e81eb34a8c0d47041a26)), closes [#283](https://github.com/mdopp/solbay/issues/283)
* **template:** SOUL.md grounds device/state questions in live HA tool calls ([1e25a43](https://github.com/mdopp/solbay/commit/1e25a4360cadfc8c78beb491161a5ba09532f03d)), closes [#276](https://github.com/mdopp/solbay/issues/276)

## [0.4.1](https://github.com/mdopp/solbay/compare/v0.4.0...v0.4.1) (2026-06-08)


### Bug Fixes

* **chat,template:** per-turn time grounding, SOUL.md scanner, compaction title collision ([a5dd663](https://github.com/mdopp/solbay/commit/a5dd6631103d0a80201c30567f56ac2cd446734b))
* **chat:** drive pinned household row highlight from selection state ([adabd35](https://github.com/mdopp/solbay/commit/adabd35bda23d9dc308084ec54847f7afa22cc33))
* **chat:** drive pinned household row highlight from selection state ([1d270de](https://github.com/mdopp/solbay/commit/1d270ded2a4ca0125936df8449f3f0c5d996a7be)), closes [#262](https://github.com/mdopp/solbay/issues/262)
* **chat:** give compaction continuation a unique title ([76790dc](https://github.com/mdopp/solbay/commit/76790dce633f250ac1343f68e0283ebdac2f0674)), closes [#267](https://github.com/mdopp/solbay/issues/267)
* **template:** ground Hermes time per-turn and de-trigger SOUL.md scanner ([4db2522](https://github.com/mdopp/solbay/commit/4db25226511de45b7e2e769093226d32f2f91250)), closes [#265](https://github.com/mdopp/solbay/issues/265) [#266](https://github.com/mdopp/solbay/issues/266)

## [0.4.0](https://github.com/mdopp/solbay/compare/v0.3.0...v0.4.0) (2026-06-08)


### Features

* **ci:** add workflow_dispatch to build-images + post-release trigger in release-please ([d02bbd2](https://github.com/mdopp/solbay/commit/d02bbd28f1f3ad82b9f2e2dfaac31266e42847cb))
* **ci:** add workflow_dispatch to build-images + post-release trigger in release-please ([5fff7e8](https://github.com/mdopp/solbay/commit/5fff7e8d0063362d06feafd77fd50773316423b6)), closes [#256](https://github.com/mdopp/solbay/issues/256)


### Bug Fixes

* **chat:** surface tool-turn reply text instead of an empty bubble ([7b16abb](https://github.com/mdopp/solbay/commit/7b16abb1bfe98fef7393b1db26b3cd073c30fcf9))
* **chat:** surface tool-turn reply text instead of an empty bubble ([380ea52](https://github.com/mdopp/solbay/commit/380ea52350e39b57daeda4c432200d4a00e4ef79)), closes [#258](https://github.com/mdopp/solbay/issues/258)

## [0.3.0](https://github.com/mdopp/solbay/compare/v0.2.0...v0.3.0) (2026-06-08)


### Features

* **chat:** adaptive + selectable reasoning routing with rendered thinking ([e98a825](https://github.com/mdopp/solbay/commit/e98a825fd8ea1a360a000f7268d0960908aeebec)), closes [#222](https://github.com/mdopp/solbay/issues/222) [#224](https://github.com/mdopp/solbay/issues/224)
* **chat:** adaptive context window from live Ollama model ([41505d4](https://github.com/mdopp/solbay/commit/41505d48ecd8fd9f45e01840a94c5d95b879bea5))
* **chat:** assign chat topics — session_topics table + picker + chip ([119c131](https://github.com/mdopp/solbay/commit/119c1315469f5017f147428628a5f2c7f55f881d)), closes [#241](https://github.com/mdopp/solbay/issues/241)
* **chat:** auto-tag ingestion with #topic/&lt;slug&gt; from the active topic ([1f48d8b](https://github.com/mdopp/solbay/commit/1f48d8b2d2f02c44c341bab1dcdae9e7abbad383)), closes [#243](https://github.com/mdopp/solbay/issues/243)
* **chat:** bind a topic's default model and persona at session create ([44e4f6a](https://github.com/mdopp/solbay/commit/44e4f6ae7390f460b0278f5705c45cd876310cd9)), closes [#242](https://github.com/mdopp/solbay/issues/242)
* **chat:** chat compaction — extract durable learnings, then compact [#210](https://github.com/mdopp/solbay/issues/210) ([3e872b5](https://github.com/mdopp/solbay/commit/3e872b5bee3800963ee25596070355b0c5299be0))
* **chat:** derive compaction context window from the live Ollama model ([dde868f](https://github.com/mdopp/solbay/commit/dde868f34de997a47d286b41c1e63855381af00e)), closes [#235](https://github.com/mdopp/solbay/issues/235)
* **chat:** embed cosmetics + CSP frame-ancestors ([89a7b6f](https://github.com/mdopp/solbay/commit/89a7b6fa80e0f818a5f4c1520b1d6e5cf8aa9b8c))
* **chat:** embed cosmetics, accent theme override, focused layout ([8cdf9d4](https://github.com/mdopp/solbay/commit/8cdf9d4c0daae213d1e3ef34bd442d9e365b3c7b)), closes [#227](https://github.com/mdopp/solbay/issues/227)
* **chat:** mobile rail/footer/composer polish cluster ([9d06b1c](https://github.com/mdopp/solbay/commit/9d06b1c7b6b23f924d73bacc5f7079c4b6b2e606))
* **chat:** mobile rail/footer/composer polish cluster ([22a64f6](https://github.com/mdopp/solbay/commit/22a64f61c24ac798a901e3aa0606844c968d08b4)), closes [#217](https://github.com/mdopp/solbay/issues/217) [#216](https://github.com/mdopp/solbay/issues/216) [#213](https://github.com/mdopp/solbay/issues/213) [#212](https://github.com/mdopp/solbay/issues/212) [#211](https://github.com/mdopp/solbay/issues/211)
* **chat:** per-turn latency trace waterfall under each reply ([397be99](https://github.com/mdopp/solbay/commit/397be99929ad83604df7731fd4e74f3ac871de61)), closes [#225](https://github.com/mdopp/solbay/issues/225)
* **chat:** server-side ServiceBay-maintenance persona lock ([ec8b4a3](https://github.com/mdopp/solbay/commit/ec8b4a3ae62a519e7f6888f9f92db4dc62121add))
* **chat:** server-side ServiceBay-maintenance persona lock ([14e5c08](https://github.com/mdopp/solbay/commit/14e5c0874f0bc497d2df0affa00497a74850698b)), closes [#229](https://github.com/mdopp/solbay/issues/229)
* **chat:** temporary/incognito chat — ephemeral by default with extract-to-topic escape hatch ([b88019c](https://github.com/mdopp/solbay/commit/b88019ce21a46f1f7b8a72972aa1411aa0052ba0)), closes [#246](https://github.com/mdopp/solbay/issues/246)
* **chat:** topic-filtered retrieval — notes-search by topic + topic dashboard ([3c5c9e3](https://github.com/mdopp/solbay/commit/3c5c9e3f2030c3041ce969558c7fdc90ec30b2cb)), closes [#244](https://github.com/mdopp/solbay/issues/244)
* **latency:** e2b/12b model routing, 128k window, keep-alive, conservative toolset trim ([f19bad9](https://github.com/mdopp/solbay/commit/f19bad96dc3fd185d5a71a26fe4f25a815b11629))
* **skill:** admin operator soul skill pack ([b787e69](https://github.com/mdopp/solbay/commit/b787e69499bb2dc891a00b54017192346efdb063))
* **skill:** admin operator soul skill pack ([ec025b8](https://github.com/mdopp/solbay/commit/ec025b8c3a86e8cabb30b5000e1ee65174d6e340)), closes [#176](https://github.com/mdopp/solbay/issues/176)
* **skill:** topic suggestion — propose a topic for a recurring theme, create on confirm ([59eb9e7](https://github.com/mdopp/solbay/commit/59eb9e75c2e0cc19363ac039b3172446e5e55d05)), closes [#245](https://github.com/mdopp/solbay/issues/245)
* **solbay:** full-bleed chat composer bar ([f7cf2bb](https://github.com/mdopp/solbay/commit/f7cf2bbbb463b4e9a627a5ade7cddf515ee1d0db))
* **solbay:** full-bleed chat composer bar ([312a043](https://github.com/mdopp/solbay/commit/312a043ae1c42d99d26aaf0ee194acbfa0e73399)), closes [#201](https://github.com/mdopp/solbay/issues/201)
* **solilos-chat:** add pinned household chat pre-bound to household topic ([49a16f4](https://github.com/mdopp/solbay/commit/49a16f4c3e15ae05c6484101ae1f3a28d332d730)), closes [#237](https://github.com/mdopp/solbay/issues/237)
* **template:** adaptive e2b/12b model routing, 128k window, keep-alive, wider toolset trim ([b9b8e00](https://github.com/mdopp/solbay/commit/b9b8e00c4c95a9d17e69adc098d102d9644f1123))
* **template:** add FRAME_ANCESTORS CSP frame-ancestors for the chat embed ([8e2fa57](https://github.com/mdopp/solbay/commit/8e2fa575cfc7db7ec1c2d14643d13605c60b7f43)), closes [#228](https://github.com/mdopp/solbay/issues/228)
* **template:** auto-install HA Jellyfin integration via config-flow API ([8977b66](https://github.com/mdopp/solbay/commit/8977b667423781c91f1a575e36bb8817c08b550a))
* **template:** auto-install HA Jellyfin integration via config-flow API ([0f0fbb4](https://github.com/mdopp/solbay/commit/0f0fbb45148937297cc9b64a114656514c38f407)), closes [#195](https://github.com/mdopp/solbay/issues/195)
* **template:** tune ollama gemma4:12b efficiency and add dedicated embed model ([e014a91](https://github.com/mdopp/solbay/commit/e014a91047662191b0e027fcd678c62a6d2c487b)), closes [#214](https://github.com/mdopp/solbay/issues/214)
* **topics:** Topics v1 epic ([#239](https://github.com/mdopp/solbay/issues/239)) + temporary chat ([c815245](https://github.com/mdopp/solbay/commit/c815245ecdba343b71aa879164d0d987f01d9ce2))


### Bug Fixes

* **chat:** render reasoning from Hermes reasoning_content, not a thinking tag ([9a81914](https://github.com/mdopp/solbay/commit/9a819148f79e933f95ad7ca00123ad4d7af859d8)), closes [#231](https://github.com/mdopp/solbay/issues/231)
* **chat:** show the Solilos release version in the sidebar badge ([fef817e](https://github.com/mdopp/solbay/commit/fef817e96ec87d53d687375e33e4b01684fd451a)), closes [#223](https://github.com/mdopp/solbay/issues/223)
* **ci:** inject git-describe as SOLILOS_VERSION for the chat badge ([af6efdd](https://github.com/mdopp/solbay/commit/af6efdd9ac3be1eb7bdd8b51e8de49b20a883d03)), closes [#248](https://github.com/mdopp/solbay/issues/248)
* **solbay:** single rail divider and untiled in-app Sol mark ([da081cd](https://github.com/mdopp/solbay/commit/da081cd82471fb8b6e709ed6a351c0d5cf2d6cea)), closes [#219](https://github.com/mdopp/solbay/issues/219) [#220](https://github.com/mdopp/solbay/issues/220)
* **solilos-chat:** skip trivial device-control turns in compaction extract ([6de38ff](https://github.com/mdopp/solbay/commit/6de38ff5c11bc4f9b9b79f02f26461231833a472)), closes [#250](https://github.com/mdopp/solbay/issues/250)
* **template:** disable unused Hermes toolsets to shrink cold-cache prefill ([9caadcd](https://github.com/mdopp/solbay/commit/9caadcd6ef050ae6ddaf160c72274b16e226ec5c)), closes [#230](https://github.com/mdopp/solbay/issues/230)
* **template:** mount solilos.db and notes vault into solilos-chat pod ([2367ed9](https://github.com/mdopp/solbay/commit/2367ed95b50ab74d1b71ce7dacd81fc06b4c155a))
* **template:** mount solilos.db and notes vault into solilos-chat pod ([2c8b680](https://github.com/mdopp/solbay/commit/2c8b680d002772f611b4dd0207ab25bc48866d67))
* **template:** re-enable household toolsets, keep only clearly-unused disabled ([a46640f](https://github.com/mdopp/solbay/commit/a46640f9cb3c592099c7c5044378ffc65ba1b0ec))

## Changelog

This changelog is maintained by [release-please](https://github.com/googleapis/release-please)
from the conventional commits on `main`. Release history before this file is in
the [GitHub releases](https://github.com/mdopp/solbay/releases).
