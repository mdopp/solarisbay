# Changelog

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
