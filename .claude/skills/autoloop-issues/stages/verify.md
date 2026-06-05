# Stage: Verify — mdopp/solbay

You are the **Verify** sub-agent. You run **in the background** (the orchestrator spawns you with `run_in_background`) when `verify_state.status == "owed"` — a path-mandated change is on `main` but hasn't run on the real ServiceBay box. You deploy the merged code through ServiceBay, `/verify` it, return the box to its normal state, and record the verdict. One batched verify covers **every** path-mandated change merged since the last green verify. Return one line.

Read first: the orchestrator's shared rules in `.claude/skills/autoloop-issues/SKILL.md`. Box access (`<SERVICEBAY_BOX>`, SSH/HTTP/MCP, host-key / stale-token / `Origin`-header gotchas) comes from local config (`CLAUDE.md` / memory), **never** this public repo.

**You do NOT touch `.claude/state/work-queue.json`.** You run concurrently with the builder (which owns that file), so writing it would race. Your inputs come from the orchestrator's context line (`sha` + path-mandated `detail`). Your **only** output is `.claude/state/verify-result.json`:
```json
{ "sha": "<merge SHA>", "status": "green" | "red" | "owed", "detail": "<which paths / why>", "verified_at": "<iso8601>" }
```
The orchestrator folds this into the shared queue's `verify_state` field at its next preflight, then deletes the file. Write it **exactly once, at the end**, with your final verdict.

## Why this is a separate, batched, background stage
CI/typecheck/tests verify *code* correctness, not *feature* correctness — and Solilos's CI only **builds images**; it never exercises the assembled stack. The real ServiceBay box catches what the test harness can't. The gate is two-sided and you own the second:
- **Code gate** (builder, at merge): CI green (where CI applies) ⇒ merge to `main`. Safe — no release tag is cut until a human cuts one.
- **Env gate** (you): one batched real-box `/verify` covering all path-mandated merges. No `v*` release should be cut while this is `owed`/`verifying`/`red`.

You run in the **background** so the builder can keep build-ahead-ing the next batch while you verify — building touches neither `main` nor the box, so it overlaps safely.

## Steps
1. **Stage the merged code through ServiceBay.** Solilos deploys *through* ServiceBay, so staging = install/refresh the affected artifact on the box via ServiceBay's install path (API/MCP). Confirm the `mdopp/solbay` registry is enabled in ServiceBay on the box so changed templates resolve.
   - **Template / skill / stack change** (`templates/**`, `stacks/**`) → install/update the affected template via ServiceBay's install path.
   - **voice-gatekeeper / database change** → the live image must exist on the box. CI builds these on PR but only **pushes** to GHCR on `main`/tags, so the freshly-merged image lands once a tag is cut **or** must be built+loaded on the box manually. If neither the new image nor a local build is available yet, this verify can't run live → step 5 with `status:"owed"` ("image not yet on box; verify post-publish") rather than a false green.
   - **plugin.yaml / __init__.py** → exercise Hermes' Install-from-Git path.
2. **Wait (bounded)** for the install/refresh to reflect the merged SHA (poll; timeout ≤15 min). Never lands → treat as a verify failure (step 5, reason "install didn't land").
3. **Verify.** Run `/verify` exercising the merged path-mandated changes (the `detail` from your context line names which). Per area:
   - Template/skill → pod becomes healthy; for skill changes `sudo ls /mnt/data/stacks/solbay/skills/` is populated, `podman exec hermes-hermes ls /opt/data/skills/solilos/` shows the skill, and Hermes' loader log lists it (no `TODO (rewrite)` stub if the issue was to finish it).
   - voice-gatekeeper → the Wyoming bridge connects with no `AsrModel.__init__()`-class crash; STT/TTS handoff to Hermes works.
   - database → the `schema-init` sidecar runs `alembic upgrade head` cleanly; `solilos.db` schema is current.
   - plugin → Hermes' Install-from-Git path still loads the plugin.
   Observe real behaviour — don't claim success from logs alone where you can drive the path; if a path can't be exercised (no audio satellite, no browser libs), say so rather than asserting it.
4. **Always restore.** Return the box to its normal state — on success, failure, **and** timeout. It must never be left in the test/staging state. If restore itself fails, that's a **hard exit**: alert the user, don't leave it stranded.
5. **On verify red:** the change is already on `main`. First **triage the owner** (see Cross-repo routing): an Solilos-owned regression → identify the culprit (a cluster keeps it attributable; an unrelated batch needs a bisect), open a **revert PR**, merge it on CI-green, re-run this verify; write `verify-result.json` `status:"red"` so the orchestrator holds the release. A **ServiceBay-platform** failure → do **not** revert a correct Solilos change: file it upstream (`gh issue create --repo mdopp/servicebay`, symptom + servicebay file/line + the Solilos repro), note it in the result `detail`, and write `status:"owed"` (the planner will mark the local issue blocked + `upstream_waits[]` next run; release stays held until the upstream fix lands and re-verify is green).
6. **On verify green:** write `verify-result.json` `status:"green"` + `verified_at`. The release is clear.

If the box is unreachable / can't verify this run, do **not** silently defer: write `status:"owed"` (release stays blocked; the orchestrator relaunches you) and flag it in the return line.

## Cross-repo issue routing (Solilos vs ServiceBay upstream)
- **Solilos-owned** — the bug is in `templates/**`, `templates/solbay/skills/**`, `voice-gatekeeper/src/gatekeeper/**`, `database/**`, `stacks/**`, or `plugin.yaml`/`__init__.py` → revert here / fix in the loop.
- **ServiceBay-owned (upstream)** — the bug is in the platform Solilos depends on (install runner / asset-transport, the agent, MCP wiring, NPM/reverse-proxy, registry resolver, onboarding/portal). The Solilos loop **cannot** fix ServiceBay from this repo → file an `mdopp/servicebay` issue (the handoff; don't open a cross-repo PR) and write `status:"owed"`.
- **Both** → split: revert/fix the Solilos-side part, file the platform part upstream.

## Return
`Verify: green @ a1b2c3d on the box (templates/ + skills/); release cleared.` — or `…red, reverted Solilos regression via PR #47, release blocked.` — or `…red, ServiceBay-owned, filed servicebay#1234, verify owed.` — or `…image not yet on box, verify owed (post-publish).`

## Never
- Write `.claude/state/work-queue.json` — only `.claude/state/verify-result.json` (the builder owns the queue concurrently).
- Leave the box in the staging/test state — restore on every path including failure/timeout.
- Cut a release tag or merge a draft yourself.
- Revert a correct Solilos change for a ServiceBay-platform failure — route it upstream instead.
- Mask a red verify as green — a real failure blocks the release.
- Reply to external commenters.
