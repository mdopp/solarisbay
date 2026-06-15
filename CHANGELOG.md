# Changelog

## [0.14.0](https://github.com/mdopp/solarisbay/compare/v0.13.0...v0.14.0) (2026-06-15)


### Features

* **chat:** /persona and /model commands, drop Settings button + persona dropdown ([788e840](https://github.com/mdopp/solarisbay/commit/788e8405ceda50276aa5a06d18bf152d221993c7)), closes [#420](https://github.com/mdopp/solarisbay/issues/420)
* **chat:** structured LLM-call trace modal + usable skill/soul editor ([dae3e63](https://github.com/mdopp/solarisbay/commit/dae3e63cacecc86ba32a7046da4bba3f7338c146)), closes [#416](https://github.com/mdopp/solarisbay/issues/416) [#418](https://github.com/mdopp/solarisbay/issues/418)
* **chat:** wire dynamic-skills promotion to the generic SB approval API ([5a0f6ce](https://github.com/mdopp/solarisbay/commit/5a0f6ce98296b368cc34cd6f2f24b704c143efa9)), closes [#427](https://github.com/mdopp/solarisbay/issues/427)
* dynamic-skills SB-approval promotion + custom Solaris wake-word wiring ([e1b78f2](https://github.com/mdopp/solarisbay/commit/e1b78f28d3d91586a876519ef266267982982549))
* **template:** wire custom "Solaris" wake word + ship model-gen recipe ([9868db6](https://github.com/mdopp/solarisbay/commit/9868db682d05e0640171afb136b5302a142cb5c3)), closes [#407](https://github.com/mdopp/solarisbay/issues/407)


### Bug Fixes

* **chat:** household slash commands, /help parity, single durable household session ([9281c3d](https://github.com/mdopp/solarisbay/commit/9281c3df5b77873ce42fcd8b3f5970f92a496f22)), closes [#421](https://github.com/mdopp/solarisbay/issues/421) [#417](https://github.com/mdopp/solarisbay/issues/417) [#419](https://github.com/mdopp/solarisbay/issues/419)
* **db:** seed debug_mode from SOLARIS_DEBUG_MODE_DEFAULT env ([ffc9748](https://github.com/mdopp/solarisbay/commit/ffc9748ed4c92df3404642dab76b66bb1bec0388)), closes [#432](https://github.com/mdopp/solarisbay/issues/432)
* **engine:** containment-check skill promotion path against symlink escape ([7f7837e](https://github.com/mdopp/solarisbay/commit/7f7837e4397f2f9f358963def6c360998e278148))
* **engine:** containment-check skill promotion path against symlink escape ([b0cbfa4](https://github.com/mdopp/solarisbay/commit/b0cbfa472bb063f0de411a79d362628761e5b92c)), closes [#439](https://github.com/mdopp/solarisbay/issues/439)
* **gatekeeper:** gate enrolment listing and scope session delete by owner ([78a21fb](https://github.com/mdopp/solarisbay/commit/78a21fbe2e437421a19efdeee6e5ba2e70bfc3c0)), closes [#437](https://github.com/mdopp/solarisbay/issues/437) [#438](https://github.com/mdopp/solarisbay/issues/438)
* **gatekeeper:** serialise concurrent same-uid enroll capture ([5a3bbb0](https://github.com/mdopp/solarisbay/commit/5a3bbb0f9e23772abf5361976168684765efeaf9)), closes [#441](https://github.com/mdopp/solarisbay/issues/441)
* post-Hermes skill cleanup + debug_mode default from env ([e2a3357](https://github.com/mdopp/solarisbay/commit/e2a3357f708db2acadce55b8994a3e6319273450))
* **security:** auth-gate /enrolments + owner-gate session delete ([#437](https://github.com/mdopp/solarisbay/issues/437), [#438](https://github.com/mdopp/solarisbay/issues/438)) ([0e3cb56](https://github.com/mdopp/solarisbay/commit/0e3cb562826b483ae0107aa2f1fbec6d987b67db))
* **skill:** replace stale Hermes tool names and drop dead hermes-api check ([91c001d](https://github.com/mdopp/solarisbay/commit/91c001df059128d3a25ec9076f2b28511c65e7dc)), closes [#431](https://github.com/mdopp/solarisbay/issues/431) [#433](https://github.com/mdopp/solarisbay/issues/433)
* **skill:** update admin-diagnose and admin-logs container name map ([c35ddc7](https://github.com/mdopp/solarisbay/commit/c35ddc7a8f4a028613a8b50ccff42769765e9042)), closes [#430](https://github.com/mdopp/solarisbay/issues/430)
* **template:** drop {{VAR}} comment false-positive, clarify HA token absence ([9b733dd](https://github.com/mdopp/solarisbay/commit/9b733dd3e6f213dca0fd96f0b07bc6f134384090)), closes [#424](https://github.com/mdopp/solarisbay/issues/424) [#425](https://github.com/mdopp/solarisbay/issues/425)
* **template:** forward GATEKEEPER_URL and PUSH_TOKEN to the chat container ([912eb95](https://github.com/mdopp/solarisbay/commit/912eb95ca92f351ca2795753fafd0104308b57bf)), closes [#440](https://github.com/mdopp/solarisbay/issues/440)
* **template:** robustify HA long-lived token adoption ([6c40065](https://github.com/mdopp/solarisbay/commit/6c4006573071a87bb11d6938437b0d8f21b91fdd))
* **template:** robustify HA long-lived token adoption ([717b9f5](https://github.com/mdopp/solarisbay/commit/717b9f576b364f28d17ad6e76c8b2d11b15f7a42)), closes [#425](https://github.com/mdopp/solarisbay/issues/425)
* wire gatekeeper push into chat container + serialise enroll capture ([b519532](https://github.com/mdopp/solarisbay/commit/b519532e36684bbc2c822d923e20d2c8dd8e3e1d))

## [0.13.0](https://github.com/mdopp/solbay/compare/v0.12.1...v0.13.0) (2026-06-15)


### Features

* **chat:** anchor the transcript to the bottom, fix mobile keyboard gap ([c22a04c](https://github.com/mdopp/solbay/commit/c22a04cbbf57cbd008fef859b07c57a1b0cecbf7)), closes [#412](https://github.com/mdopp/solbay/issues/412)
* **chat:** graphical VRAM headroom from ServiceBay's real GPU numbers ([#401](https://github.com/mdopp/solbay/issues/401)) ([8f9fd5b](https://github.com/mdopp/solbay/commit/8f9fd5b0e308413a3857a6e736b416ac8b17dbbb))
* **chat:** guide self-enrolment by name not technical uid ([#399](https://github.com/mdopp/solbay/issues/399)) ([2270373](https://github.com/mdopp/solbay/commit/2270373cae754118514eb8dc68a6e88ab571894d)), closes [#396](https://github.com/mdopp/solbay/issues/396)
* **chat:** move settings + search into inline command cards, merge mobile header ([0be8424](https://github.com/mdopp/solbay/commit/0be8424c4f9a036a70bb9c2102b04a8d87762292)), closes [#410](https://github.com/mdopp/solbay/issues/410) [#411](https://github.com/mdopp/solbay/issues/411)
* **chat:** self-service voice enrolment for the household profile ([#397](https://github.com/mdopp/solbay/issues/397)) ([10e9013](https://github.com/mdopp/solbay/commit/10e90137e4e3411d70c34dddba1b9a0362806c84)), closes [#396](https://github.com/mdopp/solbay/issues/396)
* **chat:** voice onboarding reliability and chat-UI command-card consolidation ([1fa1717](https://github.com/mdopp/solbay/commit/1fa171748a3686f614a967d729f3aa8f6389f377))
* **skill:** pin the self-enrolment step order in the household soul ([#400](https://github.com/mdopp/solbay/issues/400)) ([10071e6](https://github.com/mdopp/solbay/commit/10071e61f2ee02f382c3e6413caef4b7a6cc6fcb)), closes [#396](https://github.com/mdopp/solbay/issues/396)
* **skill:** rename the household persona to Solaris, enrol on sentences ([#403](https://github.com/mdopp/solbay/issues/403)) ([f99659b](https://github.com/mdopp/solbay/commit/f99659b8c5952506371630d409e64b4573663b60)), closes [#396](https://github.com/mdopp/solbay/issues/396)
* **solaris:** rebrand stage 2 — coordinated repo-wide artifact rename + draft BRAND.md ([75f3818](https://github.com/mdopp/solbay/commit/75f3818c98e87ee2c05d915ebf33d8ad09cc2d12))
* **solaris:** rebrand stage 2 — rename artifacts solilos→solaris, solbay→solarisbay ([1b6530a](https://github.com/mdopp/solbay/commit/1b6530a0aef4bdd7ae46ad77d0d37b4347947be6)), closes [#408](https://github.com/mdopp/solbay/issues/408)
* **template:** switch the household fast model to gemma4:e4b ([#402](https://github.com/mdopp/solbay/issues/402)) ([ccb197c](https://github.com/mdopp/solbay/commit/ccb197ce46301f26a1c8bdc6c826b78c47fbfa19))


### Bug Fixes

* **chat:** fold the turn trace into one step list, drop the redundant block ([102bc86](https://github.com/mdopp/solbay/commit/102bc868e28d086c3e308fd7700f3713bf777c9b)), closes [#406](https://github.com/mdopp/solbay/issues/406)
* **chat:** hand the model the exact 3-sentence enrol prompt to echo ([f4de2d6](https://github.com/mdopp/solbay/commit/f4de2d6865a55cc1ab105a364ae246c5b5369de3)), closes [#404](https://github.com/mdopp/solbay/issues/404)
* **chat:** persist a voice turn's trace into its durable Zuhause session ([f6c0979](https://github.com/mdopp/solbay/commit/f6c0979256c69393e44efe7503d87177efc2dc7d)), closes [#405](https://github.com/mdopp/solbay/issues/405)
* **chat:** remove the activity bubble on a stopped turn ([f1fc218](https://github.com/mdopp/solbay/commit/f1fc218a4feec42307feb6a8b541a808a0bace5b))
* **chat:** remove the live activity bubble on a stopped turn ([d411611](https://github.com/mdopp/solbay/commit/d411611be3f6e116b0eeb0465173d2d47f3883ae)), closes [#414](https://github.com/mdopp/solbay/issues/414)
* **template:** wait for gatekeeper Wyoming STT before wiring it as Assist STT ([8aaee5e](https://github.com/mdopp/solbay/commit/8aaee5e2821d339ce70f7f0bdf0f0baaa819a2d1)), closes [#395](https://github.com/mdopp/solbay/issues/395)

## [0.12.1](https://github.com/mdopp/solbay/compare/v0.12.0...v0.12.1) (2026-06-13)


### Bug Fixes

* **chat:** surface enroll STATUS_FAILED honestly and clear the stale row ([f36fe38](https://github.com/mdopp/solbay/commit/f36fe3841429d7956f6a01935dcfcd786d223d48)), closes [#389](https://github.com/mdopp/solbay/issues/389)
* **chat:** two eval-found bugfixes — admin MCP panel + honest enroll failure ([ba9553a](https://github.com/mdopp/solbay/commit/ba9553a4ab17dbe49e6d0ebbf31557b4bd452d5e))
* **chat:** unwrap CombinedToolbox in admin MCP panel introspection ([3694067](https://github.com/mdopp/solbay/commit/36940676ec11d6df50c57aa908b1bcc590ee3658)), closes [#390](https://github.com/mdopp/solbay/issues/390)

## [0.12.0](https://github.com/mdopp/solbay/compare/v0.11.0...v0.12.0) (2026-06-13)


### Features

* add a global Kokoro voice picker like the model picker ([453ecd1](https://github.com/mdopp/solbay/commit/453ecd1038755eb593a68735f3aa07827589ef7b)), closes [#368](https://github.com/mdopp/solbay/issues/368)
* answer "wer bin ich?" from the resident voice-ID belief ([2ab8ec5](https://github.com/mdopp/solbay/commit/2ab8ec527200571066b4b6b1854078c8490a917f))
* answer "wer bin ich" from the voice-ID belief ([372090d](https://github.com/mdopp/solbay/commit/372090dafeb0150cb8aaf4980fe1cff5b9acd984)), closes [#384](https://github.com/mdopp/solbay/issues/384)
* **chat:** confirm home-securing actions, act decisively otherwise ([971585d](https://github.com/mdopp/solbay/commit/971585d3364b5c6ccd0f41ab0904d75dbb1668fa)), closes [#382](https://github.com/mdopp/solbay/issues/382)
* **chat:** HA service legend in registry + home-securing confirmation policy ([a96996b](https://github.com/mdopp/solbay/commit/a96996b5df4065e7f8c49680804f5c358286669d))
* **chat:** HA state-history search and list/run scenes scripts automations ([0230b5d](https://github.com/mdopp/solbay/commit/0230b5d262440fc98100692870c03de6dda120b9)), closes [#369](https://github.com/mdopp/solbay/issues/369) [#370](https://github.com/mdopp/solbay/issues/370)
* **chat:** handle denied access requests — drop biometric, provision nothing ([6f12e7f](https://github.com/mdopp/solbay/commit/6f12e7f9d69b2e01cf94c195736e68f01dde1324))
* **chat:** inject per-domain HA service legend into the entity registry ([20d120c](https://github.com/mdopp/solbay/commit/20d120c8012fec50e0d999bee1217a360f0e7cfe)), closes [#381](https://github.com/mdopp/solbay/issues/381)
* **chat:** make the household profile model admin-selectable in the picker ([a7003f5](https://github.com/mdopp/solbay/commit/a7003f5d01d52e940ac8b2e1e40ecda2f44d0dbd)), closes [#366](https://github.com/mdopp/solbay/issues/366)
* **chat:** onboarding approval + provisioning on SB access-request MCP ([9ca171f](https://github.com/mdopp/solbay/commit/9ca171f62d41a889a3c5b11141bcf74ebb8925ea))
* **chat:** onboarding approval + provisioning on SB access-request MCP ([10d605b](https://github.com/mdopp/solbay/commit/10d605b6d48e07994156b5be45429c7eb84d5b1c)), closes [#355](https://github.com/mdopp/solbay/issues/355)
* **chat:** panel model management + Kokoro voice picker ([846f119](https://github.com/mdopp/solbay/commit/846f119c42d04d5818ee5bfef62da2fddf6288bb))
* **chat:** registration flow — enrol voice + file pending resident request ([b4c3d80](https://github.com/mdopp/solbay/commit/b4c3d80bbf4f758cc8735d7fb73e502bc2b67a88))
* **chat:** registration flow — enrol voice + file pending resident request ([38f2de2](https://github.com/mdopp/solbay/commit/38f2de2f76b8a9c4ed97019d3b3a7941ac0dcd67)), closes [#376](https://github.com/mdopp/solbay/issues/376)
* **chat:** voice-enrolment tool wrapping gatekeeper POST /enrol ([0cd9cc9](https://github.com/mdopp/solbay/commit/0cd9cc92452c947b6e3944a1329f5d7994e2d6e3))
* **chat:** voice-enrolment tool wrapping gatekeeper POST /enrol ([0cbcab9](https://github.com/mdopp/solbay/commit/0cbcab9ceefa927f919066dece29f9f9ae4168bc)), closes [#364](https://github.com/mdopp/solbay/issues/364)
* **engine:** personalize turn prompt with the resident identity ([f2876dc](https://github.com/mdopp/solbay/commit/f2876dcdfac9f40c76e927cb585bc8e5490957db)), closes [#352](https://github.com/mdopp/solbay/issues/352)
* **gatekeeper:** reverse enroll-stash — live-voice onboarding capture ([7c508ca](https://github.com/mdopp/solbay/commit/7c508caeb46bbfafc459b7bcfacfc0de426dd1b7))
* **gatekeeper:** reverse enroll-stash for live-voice onboarding capture ([cfb446a](https://github.com/mdopp/solbay/commit/cfb446a54029bdc379c7847712620fd5952aed24))
* pull Ollama models from the panel + VRAM headroom estimate ([40410f5](https://github.com/mdopp/solbay/commit/40410f5ee2a34e0b1b9001ef49567e11c0aacb05)), closes [#367](https://github.com/mdopp/solbay/issues/367)
* resident-personalized turns, trace tool-step fix, HA history+run tools, admin-selectable household model ([ac709c1](https://github.com/mdopp/solbay/commit/ac709c1346e58d0512a3e4587b87bbf3ec133da4))
* route an unknown speaker to the guest profile ([5f14ae9](https://github.com/mdopp/solbay/commit/5f14ae9db6b151d74f3883bda1a1be9a125dca02))
* route an unknown speaker to the guest profile ([ea85395](https://github.com/mdopp/solbay/commit/ea853951c4e728fa602a6789e9025369194e0bb5)), closes [#351](https://github.com/mdopp/solbay/issues/351)
* **skill:** add the resident-registration onboarding dialog ([e9a4b1d](https://github.com/mdopp/solbay/commit/e9a4b1d07b0fe1606c3cfd16bf9ecf8927ad613e)), closes [#354](https://github.com/mdopp/solbay/issues/354)
* **skill:** guest-greeting onboarding dialog for unknown speakers ([f1ca148](https://github.com/mdopp/solbay/commit/f1ca14810dcaf9d82ccc3ca5e504bceae98d023a))
* **skill:** guest-greeting onboarding dialog for unknown speakers ([3d50acf](https://github.com/mdopp/solbay/commit/3d50acf143f91eb5c20fa4be72fa020b779d366d)), closes [#375](https://github.com/mdopp/solbay/issues/375)
* **skill:** resident-registration onboarding dialog ([ca96960](https://github.com/mdopp/solbay/commit/ca969604e251458752fd788c2614a6051b336c7e))
* **voice:** wire gatekeeper speaker-ID into the live Assist path ([#350](https://github.com/mdopp/solbay/issues/350), approach b) ([#362](https://github.com/mdopp/solbay/issues/362)) ([d556247](https://github.com/mdopp/solbay/commit/d5562474be2dd96464aace3d3df8c53a5966e560))


### Bug Fixes

* **chat:** drop alembic import from the pending-residents test ([efcb9d8](https://github.com/mdopp/solbay/commit/efcb9d843ef16d813c78a2c514a1fb08a4f0526e))
* **chat:** map natural cover service names to HA open_cover/close_cover/stop_cover ([90211d5](https://github.com/mdopp/solbay/commit/90211d550b44086b6d8efcc5a3bff5f47ef44020))
* **chat:** normalize natural cover service names to HA's open_cover/close_cover/stop_cover ([88045a6](https://github.com/mdopp/solbay/commit/88045a62356a0a472fe7607313e7b2cd6ef38358)), closes [#379](https://github.com/mdopp/solbay/issues/379)
* **chat:** render persisted tool trace steps as the tool, expand by default ([1167276](https://github.com/mdopp/solbay/commit/116727676ae51b4b7a002a9464dbe8cdf6a4a993)), closes [#371](https://github.com/mdopp/solbay/issues/371)
* **db:** chain the pending-resident-request migration after enroll_requests (0015) ([7c32b8f](https://github.com/mdopp/solbay/commit/7c32b8f12bceb0bdc03523a620c1156c34872de4))

## [0.11.0](https://github.com/mdopp/solbay/compare/v0.10.0...v0.11.0) (2026-06-12)


### Features

* **chat:** alarms ring a sound instead of speaking ([040d203](https://github.com/mdopp/solbay/commit/040d2035dc1321f892abeac093617fe05a170018)), closes [#348](https://github.com/mdopp/solbay/issues/348)
* **chat:** durable household voice session + live browser mirror ([7a46fad](https://github.com/mdopp/solbay/commit/7a46fad19f95402ffe987f3a9ca810482e655fae)), closes [#345](https://github.com/mdopp/solbay/issues/345) [#344](https://github.com/mdopp/solbay/issues/344)
* **chat:** guest profile — ephemeral Q&A plus basic home control ([e2a1005](https://github.com/mdopp/solbay/commit/e2a10059eb96a7d6ccf0ba56759551540cf51815)), closes [#353](https://github.com/mdopp/solbay/issues/353)
* **chat:** live collapsed activity bubble for a turn's llm and tool steps ([8790976](https://github.com/mdopp/solbay/commit/879097669c8df9d7d23debdc6facb0a38a0f8570))
* **chat:** live collapsed activity bubble for a turn's llm and tool steps ([63d6f5e](https://github.com/mdopp/solbay/commit/63d6f5e3cc1b4bd610b2da674ea591eae31efc93)), closes [#347](https://github.com/mdopp/solbay/issues/347)
* **chat:** record tool executions as interleaved trace steps with timings ([c0b782e](https://github.com/mdopp/solbay/commit/c0b782e7ff06ddb25881fa58319dbe7a4d8f9259)), closes [#346](https://github.com/mdopp/solbay/issues/346)
* **template:** prefer the Martin TTS bridge in the Sol pipeline ([94031f3](https://github.com/mdopp/solbay/commit/94031f3f5b6ca2b9db1ccf103357cd8ba36a199d))
* **tts:** add the solilos-tts image — Kokoro-Martin German voice on GPU ([f6c8af0](https://github.com/mdopp/solbay/commit/f6c8af0a002409eb4af99ff3f84a9bec860bd320))


### Bug Fixes

* **chat:** catch perfect-tense and passive German device-action fabrications ([41c2da3](https://github.com/mdopp/solbay/commit/41c2da32128896d64bf740ce12dac2ba491b13bd))
* **chat:** catch perfect-tense device-action fabrications in the guard ([3ce1eb2](https://github.com/mdopp/solbay/commit/3ce1eb2d01f81abab74ebecc7bb816acd411514f)), closes [#360](https://github.com/mdopp/solbay/issues/360)
* **chat:** discipline last, stream-safe contextvar, uid into tool tasks ([235a4b2](https://github.com/mdopp/solbay/commit/235a4b29b150941c22b71358d9033998a7ad54e4))
* **chat:** force the tool pass when a turn fabricates a device-action claim ([afc156d](https://github.com/mdopp/solbay/commit/afc156d7f00ab334a48d9a72934e5abdc3ed076e)), closes [#356](https://github.com/mdopp/solbay/issues/356)
* **chat:** pin the tool-discipline rule at the end of the system block ([93aa7a0](https://github.com/mdopp/solbay/commit/93aa7a00fc0e7127df6ca089e49588354384ea82))
* **chat:** run the household profile at low temperature ([a293326](https://github.com/mdopp/solbay/commit/a293326e6977b94a872995bd472a05fc8c2182c4))
* **skill:** never narrate a device action — call the tool in this turn ([c6fb7a2](https://github.com/mdopp/solbay/commit/c6fb7a2070d8cba50c5beaf45164bdbaeea4482d))
* **template:** converge the pipeline on tts_voice drift too ([47581d1](https://github.com/mdopp/solbay/commit/47581d1f65e9c8702208f0f9e3570cb425c59b80))
* **template:** the Martin bridge voice is martin, not kokoro ([7438ad5](https://github.com/mdopp/solbay/commit/7438ad570ce27e72f8bf96ea2667c91793909e25))
* **template:** warm-load from the locally installed tags ([dcc8d25](https://github.com/mdopp/solbay/commit/dcc8d25988eaab02311518c9a5e48e8dad709b08)), closes [#339](https://github.com/mdopp/solbay/issues/339)
* **template:** warm-load small models first — order decides co-residency ([432e0f3](https://github.com/mdopp/solbay/commit/432e0f312f5f0dd31ff78e607d9ef975fde4053e)), closes [#340](https://github.com/mdopp/solbay/issues/340)
* **template:** warm-load the chat models after every ollama deploy ([d25a287](https://github.com/mdopp/solbay/commit/d25a287199f74e202d4ec5d768e07ebab4fe231e))

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
