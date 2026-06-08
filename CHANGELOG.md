# Changelog

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
