---
name: autoloop-issues
description: Work the open mdopp/oscar issue backlog autonomously, up to 8 PRs per invocation, with mandatory real-box `/verify` on template/skill/service/migration paths. Resumable across /loop firings via .claude/state/autoloop-state.json. Security/privacy-sensitive issues open as draft and wait for human review. Use when the user asks to "burn down the backlog", "work the oscar issues autonomously", or invokes /loop with this skill.
---

# Autoloop: OSCAR backlog burndown

You are working a queue of open GitHub issues on the **`mdopp/oscar`** repo, with explicit exit conditions and a resumable state file.

**What OSCAR is, and why verification is unusual.** OSCAR is *not* a standalone app. It is a bundle of ServiceBay artifacts:

- `templates/{hermes,hermes-webui,ollama,oscar-household}/` — ServiceBay Pod-YAML templates (`template.yml` + `variables.json` + `post-deploy.py`) installed onto a ServiceBay node.
- `templates/oscar-household/skills/*/SKILL.md` — Hermes skills, delivered to the node by ServiceBay's asset-transport and loaded by the Hermes container.
- `voice-gatekeeper/` — a Python Wyoming↔Hermes bridge. **Real service code lives in `voice-gatekeeper/src/gatekeeper/`** (entrypoint `gatekeeper.__main__:main`), with pytest in `voice-gatekeeper/tests/`. Built as Docker image `oscar-gatekeeper`. This is the most code-heavy part of the repo — bug/feature work here is normal Python work (edit `src/gatekeeper/`, extend tests, run pytest).
- `database/` — alembic schema-init sidecar (Docker image `oscar-household-init`) for `oscar.db`.
- `stacks/oscar/stack.yml` — the bundled OSCAR stack manifest.
- `plugin.yaml` + `__init__.py` — Hermes "Install-from-Git" plugin packaging.

OSCAR runs **on ServiceBay**, on the **same box ServiceBay uses: `<SERVICEBAY_BOX>`**. So real-box verification means *deploying the changed OSCAR artifact through ServiceBay onto that box and checking the OSCAR runtime* — the same test procedures the ServiceBay autoloop uses, pointed at OSCAR's pods/skills instead of ServiceBay's own. See Step 3.

This is pre-production, so the loop may merge changes across the repo — but only after CI green (where CI applies) **and**, on the path-mandated list in Step 3, only after real-box `/verify` green. Security/privacy-sensitive issues open as **draft** PRs and wait for human review.

If a project `CLAUDE.md` or user memory conflicts with this skill, those override it. Read them before the first iteration of a fresh `/loop` run.

## Per-invocation budget

- **At most 8 PRs per invocation.** Then exit cleanly. `/loop` re-fires you.
- A security/privacy-gate draft still counts as one PR's worth of work.
- If you've spent >40 minutes on a single issue without a green PR, stop, comment on the issue explaining what's blocking, and move on.

## Wakeup cadence (dynamic /loop mode)

Every `ScheduleWakeup` from this skill uses **`delaySeconds: 480` (8 minutes) or less** — including "CI running" / "nothing to do" heartbeats. Keep the backlog draining; long fallbacks stall progress when an external gate clears mid-window.

## State file

Track progress at `.claude/state/autoloop-state.json` (shape in `state-template.json`). Update it at every state transition. If absent, create it with empty arrays. Shape mirrors ServiceBay's:

```json
{
  "started": "…", "last_invocation": "…",
  "completed": [{"issue": 82, "pr": "https://github.com/mdopp/oscar/pull/…", "gate": "normal", "merged_at": "…"}],
  "in_progress": null,
  "skipped": [{"issue": 84, "reason": "security/privacy gate; draft PR #… awaiting human review"}],
  "blocked": [{"issue": 81, "reason": "needs scoping"}],
  "upstream_waits": [{"issue": 80, "servicebay_issue": "mdopp/servicebay#1234", "reason": "needs SB asset-transport fix before OSCAR side can be verified"}],
  "notes": [],
  "last_e2e": null,
  "last_codebase_eval": null
}
```

## Step 0 — Preflight (every invocation)

1. **Working tree clean?** `git status --porcelain`. If not, exit — another session is working here. Do not stash or switch branches.
2. **On `main` and up to date?** `git fetch origin && git checkout main && git pull --ff-only`. If the FF fails, exit and report.
3. **No release-please here.** Unlike ServiceBay, `mdopp/oscar` has no release-please workflow — releases are cut by pushing a `v*` tag (which triggers `build-images.yml` to publish images to GHCR). **Do not create or push tags** and do not bump versions in `pyproject.toml` unless the user explicitly asks. There is no release PR to merge in preflight.
4. **Lock check.** Read `.claude/scheduled_tasks.lock` if present; if another invocation ran within the last 10 minutes, exit.
5. **Read state file.** Resume from `in_progress` if set; otherwise pick the next issue per the rules below.

## Step 1 — Issue selection

```bash
gh issue list --repo mdopp/oscar --state open --limit 100 --json number,title,labels,body
```

### Exclusion filter (drop any issue matching any of these)

- Labels include any of: `postponed`, `wontfix`, `duplicate`, `invalid`, `autoloop-open`.
- Issue number appears in `state.completed[]`, `state.skipped[]`, or `state.blocked[]`.
- Title/body is clearly multi-PR scope ("audit", "strategy", "epic", or work spanning many changes). Mark `blocked` with reason `"needs scoping"` and move on — *or*, in a refine pass (track b) or when the user asks, **decompose** it into bite-sized child issues (see track b's "Decomposing an epic") and keep this one open as the tracking umbrella. (Many OSCAR feature issues — daily-journal, wiki-linking, media-availability — are genuinely multi-PR; decomposing them is usually better than parking them.)

### Classification (everything that survives)

- **Security/privacy gate** — the issue touches biometric speaker-ID, gateway credentials (Signal/Telegram/Discord), Honcho per-resident privacy, long-lived HA/MCP tokens, or anything labelled `security`. Open the code PR as **draft**; the loop never merges it. Label the issue `autoloop-open`, add to `state.skipped[]` with reason `"security/privacy gate; draft PR #X awaiting human review"`. (If no `security` label exists yet, create one: `gh label create security --repo mdopp/oscar --color d73a4a`.)
- **Normal flow** — everything else. Code PR, merged after CI green (where CI applies) + (if path-mandated) real-box `/verify` green.

### Selection order within survivors

1. `good first issue`
2. `bug`
3. `phase-0`, then `phase-1` (foundational phases before later enhancements)
4. `documentation`
5. Everything else, ascending issue number.

Pick the head. Update state: `in_progress = {issue, branch, gate, started_at}`.

### No eligible issues — choose a track

If Step 1 returns no survivors, **do not exit** and don't blindly default. Decide a track (same three-track model as ServiceBay):

- **a) Code hygiene** — small, focused cleanups: fix a flaky/expanded `voice-gatekeeper` or `database` test, tighten a Dockerfile, validate/normalize a `template.yml` or `SKILL.md` frontmatter, kill dead code. (OSCAR has no ESLint warning-count to drive to zero, so this track is opportunistic hygiene, not a mechanical sweep. If `ruff`/`flake8` is ever added to `voice-gatekeeper`, drive its warnings down here.)
- **b) Refine & unblock issues** — walk `state.blocked[]` + open issues; re-check whether a recent merge or a smaller scoping makes one actionable; tighten thin issue bodies (symptom + repro + starting-point files). Deliverable: a refreshed queue — then pick the head and work it.

  **Decomposing an epic** is a first-class track-b move (better than parking a multi-PR issue as `needs scoping`): break it into bite-sized child issues so the loop can ship it incrementally. Rules: each child is an independently-shippable PR-unit (foundational modules/templates first, then consumers — no dead-code-only stubs); **file them in dependency order so ascending issue number == dependency order** (the loop works ascending within a bucket, and a child whose `Depends on #N` sibling is still open is skipped until #N merges); each child body gives the deliverable + starting-point files + a `Depends on #N` line; comment the dependency DAG on the parent and keep the parent **open as the tracking epic**.
- **c) Evaluate the codebase** — run the standing eval prompt (below) against HEAD, then file the **Category 2 (Pragmatic)** findings as new issues to refill the queue (symptom-style: symptom + exact file/line + real-world consequence; no patch plan in the body). Record **Category 1 (Academic)** findings in `state.notes[]` only.
- **d) End-to-end validation on the box** — run the full "does OSCAR work within the ServiceBay install?" smoke (see the section below) and route any failures cross-repo. This is the ultimate goal of the loop; prefer it after a batch of OSCAR changes has merged.

**How to choose:** interactive → ask the operator via `AskUserQuestion`. Autonomous (`/loop`) → **(d)** if OSCAR artifacts merged since the last recorded E2E (`state.last_e2e`); else **(b)** if `state.blocked[]` is non-empty; else **(c)** if no eval recorded in the last ~5 invocations (`state.last_codebase_eval`); else **(a)**. Record the chosen track (and the E2E/eval date) in `state.notes[]` / `state.last_e2e` / `state.last_codebase_eval`.

#### Codebase-evaluation prompt (track c)

Run verbatim against the current HEAD:

```
Evaluate the OSCAR codebase across its core areas (ServiceBay Pod-YAML templates, Hermes skills/SKILL.md, the voice-gatekeeper Wyoming bridge, the database/alembic schema, the Hermes plugin packaging in plugin.yaml/__init__.py, the bundled stack manifest, and documentation).

Assume the baseline that OSCAR is a real, deployed homelab AI assistant running on a ServiceBay node. Do not give me generic style-guide complaints unless they have a direct, measurable impact on bugs or developer velocity.

CRITICAL REQUIREMENT: Findings MUST focus exclusively on active, unresolved bugs, logical flaws, security/privacy exploits, or operational dead-ends present in the current state (HEAD commit). Do NOT reference historical issues, resolved bugs, or refactors already fixed/merged in past PRs, commits, changelogs, or audits. Inspect actual active source files to verify each issue is currently live. Pay special attention to: skills whose SKILL.md still carries a TODO/stub banner; template.yml port/volume/hostNetwork wiring; voice-gatekeeper Wyoming version/constructor contracts; alembic migration correctness; and privacy handling of speaker-ID / per-resident memory / gateway credentials.

Group findings into exactly two categories:

1. Academic / Theoretical (Nice-to-Have)
Changes that satisfy clean-code metrics or theory but whose real-world ROI is near zero at this scale.

2. Pragmatic / Real-World (Should-Do)
Active, load-bearing flaws in the live code that compromise security/privacy, threaten data integrity, risk runtime/deploy crashes, or are active dead-ends that block or frustrate residents.

For each Category 2 item:
a) Exact active file(s) and line range.
b) The real-world consequence of ignoring it.
c) A brief outline of how to patch the live code.
```

After the eval: file each Category 2 finding as its own `mdopp/oscar` issue (symptom-style, no patch plan), labelled appropriately (`bug`/`skill`/`template`/`infrastructure`/`security`) so classification routes it. Then continue this invocation by selecting the head of the refilled queue, budget permitting.

## Step 2 — Implementation

### Branch
```bash
git checkout -b fix/issue-<N>-<kebab-summary>
```

### Read the issue and referenced files
- Note the starting-point files; read each fully and ~50 lines around any line reference.
- If the issue is ambiguous, comment asking the specific question and move on. **Do not guess.**

### Scope discipline
- Implement the smallest change that closes the ticket. Don't refactor neighbours or add abstractions for non-refactor tickets.
- `[Refactor]`-titled tickets stay within the file/module they name; a needed neighbouring change goes in a *separate* PR.
- "audit"/"strategy"/"epic" issues should have been marked `blocked` in Step 1.

## Step 3 — Local verification

Run what applies to the touched paths. Each must pass before the next.

**Python (`voice-gatekeeper/src/gatekeeper/` or `database/`):**
```bash
cd voice-gatekeeper && pip install -e '.[test]' >/dev/null && pytest -q   # all tests pass
# (database/ uses alembic; if it grows a tests/ dir, run it the same way)
```
The gatekeeper package is `voice-gatekeeper/src/gatekeeper/` (`__main__.py` is the entrypoint). When a bug/feature touches it, add or extend a test under `voice-gatekeeper/tests/` so the change is covered.

**Templates / skills (`templates/**`, `stacks/**`):** no compiler, so validate by hand:
- `template.yml` / `stack.yml` / `variables.json` parse as valid YAML/JSON.
- Any changed `SKILL.md` has valid frontmatter (`name`, `description`, `version`) and no leftover `TODO (rewrite)` banner if the issue was to *finish* that skill.
- Port/volume/hostNetwork wiring in `template.yml` is internally consistent (mount names match volumes; declared ports don't collide).

**Mandatory real-box `/verify`** — if the PR diff touches *any* of:
- `templates/**` (any template.yml, variables.json, post-deploy.py, or skills/)
- `stacks/**`
- `voice-gatekeeper/**`
- `database/**`
- `plugin.yaml` / `__init__.py`

then invoke `/verify` against `<SERVICEBAY_BOX>` **before merge**. OSCAR deploys *through* ServiceBay, so the procedure is:

1. Reach the box via the same SSH / HTTP-API / MCP paths ServiceBay uses (`<SERVICEBAY_BOX>`; host-key, MCP-token, and `Origin`-header gotchas are the same as ServiceBay's reference). The `mdopp/oscar` registry must be enabled in ServiceBay on that box so the changed templates resolve.
2. **Template / skill change** → install or update the affected template via ServiceBay's install path (API/MCP), then confirm: the pod becomes healthy; for skill changes, `sudo ls /mnt/data/stacks/oscar-household/skills/` is populated and `podman exec hermes-hermes ls /opt/data/skills/oscar/` shows the skill and Hermes' loader log lists it.
3. **voice-gatekeeper / database change** → the image must exist on the box to run live. CI builds these on PR but only *pushes* on `main`/tags, so a pre-merge live check needs either a locally-built+loaded image or a note that full live verification happens post-merge once GHCR has the new image. Confirm the Wyoming bridge connects (no `AsrModel.__init__()` crash class — see the wyoming pin in `voice-gatekeeper/pyproject.toml`) and the schema-init sidecar runs `alembic upgrade head` cleanly.
4. **plugin.yaml / __init__.py** → verify Hermes' Install-from-Git path still loads the plugin.

If `/verify` fails, **triage the owner before reacting** — OSCAR runs on ServiceBay, so a failure can be an OSCAR bug *or* a ServiceBay-platform bug (see "Cross-repo issue routing" below). Then: stop, post the failure summary + your owner call on the PR, leave it open, move on. Don't mock around a failing test or skip it — fix the root cause in whichever repo owns it.

**Narrow, deliberately-logged exception to the pre-merge `/verify`.** You MAY merge a path-mandated change on CI-green + a strong unit/pytest test and *defer* the real-box `/verify` — but ONLY when ALL hold: (1) the user has explicitly prioritized it, or a dependent repo is blocked waiting on it (the inverse case ServiceBay sees with OSCAR); (2) the change adds **no new runtime logic** — it reuses an already-tested, pre-existing path (removing a coercion, threading an existing-contract value, a docs/template-comment change); (3) a test covers the new behaviour; (4) you document the deferral in the PR **and** `state`, with the real-environment check to happen at the next natural opportunity (e.g. next OSCAR install on the box). Absent that explicit priority/blocked-dependent signal, the default stands: **path-mandated ⇒ `/verify` before merge.** Never apply it to a security/privacy-gated change. This is a logged judgement call, not a general loosening — when unsure, don't use it.

### Cross-repo issue routing (OSCAR vs ServiceBay upstream)

When a defect surfaces during `/verify` or end-to-end validation, decide which repo owns it:

- **OSCAR-owned** — the bug is in `templates/**`, `templates/oscar-household/skills/**`, `voice-gatekeeper/src/gatekeeper/**`, `database/**`, `stacks/**`, or `plugin.yaml`/`__init__.py`. Fix it here (this loop), or file an `mdopp/oscar` issue if it isn't bite-sized.
- **ServiceBay-owned (upstream)** — the bug is in the platform OSCAR depends on: the install runner / asset-transport, the agent, MCP wiring, NPM/reverse-proxy, the registry resolver, `config.ts`, the onboarding/portal. The OSCAR loop **cannot** fix ServiceBay from this repo. File an issue in **`mdopp/servicebay`** (symptom-style: symptom + exact file/line in servicebay + the OSCAR-side repro that exposed it), then mark the OSCAR issue `blocked` with reason `"waiting on mdopp/servicebay#<N>"` and record it in `state.blocked[]` (and `state.upstream_waits[]`). **Wait** — re-check on later invocations whether the upstream fix merged; unblock when it does.
- **Both** — split: the OSCAR-side part is fixed/filed here, the platform-side part is filed upstream and the OSCAR issue notes the dependency.

Use `gh issue create --repo mdopp/servicebay …` for upstream. Don't try to open a PR against ServiceBay from this loop — filing the issue is the handoff; the ServiceBay autoloop (or a human) works it there.

## Step 4 — Open the PR

### Commit
- Conventional Commits; scope mirrors the path: `fix(gatekeeper):`, `feat(skill):`, `fix(template):`, `feat(oscar-household):`, `chore(db):`, `docs:`.
- **No parens in the subject beyond the conventional `(scope)`** (parens-heavy subjects can break release tooling).
- Body: brief summary, then `Closes #<N>`.

### Push
```bash
git push -u origin fix/issue-<N>-<slug>
```

### PR body (write a real body — no `--fill`)
```markdown
## What
<1-2 sentences>

## Why
Closes #<N>.

## Risk
<low | medium | high — one sentence>

## Rollback
<git revert is enough | requires X>

## Verification
- [ ] pytest (if voice-gatekeeper/database touched)
- [ ] YAML/frontmatter valid (if templates/skills touched)
- [ ] /verify on <SERVICEBAY_BOX> via ServiceBay (if path-mandated — see Step 3)
```

### PR creation — by gate

**Security/privacy gate:** open `--draft`, label the issue `autoloop-open`, record in `state.skipped[]`, move on. **Do not merge.**

**Normal flow:** open the PR, then the merge gate below.

### Merge gate (normal-flow only)

`main` is likely **not** branch-protected (verify: `gh api repos/mdopp/oscar/branches/main/protection` → 404 means unprotected, so `--auto` no-ops). Use the manual gate:

1. **CI applies only to image paths.** If the diff touches `voice-gatekeeper/**`, `database/**`, or `.github/workflows/build-images.yml`, wait for CI: `gh pr checks <PR#> --watch`. **Template-only / skill-only / docs-only PRs trigger no CI** — for those the gate is local verification + `/verify`.
2. If the diff hit any path in Step 3's mandatory list, run `/verify` on the box. Block merge until green.
3. If green (CI where applicable + `/verify` where mandated): `gh pr merge <PR#> --merge --delete-branch`.
4. If CI red twice on the same SHA, or `/verify` red: stop, comment the failing link, leave the PR open, move on. Don't retry indefinitely.

Update state: move issue from `in_progress` to `completed`.

### Post-merge
```bash
git checkout main && git pull --ff-only
```
No release PR to chase. If a release is warranted, that's the user's call (push a `v*` tag) — log a suggestion in `state.notes[]`, don't tag yourself.

## Step 5 — End of invocation

After 8 PRs (merged + draft), stop. Write a summary to stdout:
```
Autoloop (oscar) iteration complete.
  Merged: #82 (PR #N), …
  Security/privacy drafts: #84 (PR #M)
  Skipped / Blocked: …
Next eligible issue: #NN.
```

## End-to-end validation: does OSCAR work within the ServiceBay install?

This is the loop's ultimate goal — per-PR `/verify` checks one change; this checks that **OSCAR actually works as a whole on the real ServiceBay box** (`<SERVICEBAY_BOX>`). Run it as track (d), after a batch of OSCAR PRs merges, or whenever the queue drains.

**Procedure (golden-path smoke, on the box via ServiceBay):**

1. Confirm the `mdopp/oscar` registry is enabled in ServiceBay on the box, then install/refresh the OSCAR stack (`stacks/oscar/stack.yml`: `oscar-household` + `hermes` + `hermes-webui` + `ollama`) through ServiceBay's install path. The install completes without errors.
2. **Skills land + load** — `sudo ls /mnt/data/stacks/oscar-household/skills/` populated; `podman exec hermes-hermes ls /opt/data/skills/oscar/` shows them; Hermes' loader log lists the OSCAR skills (no skill in a `TODO (rewrite)` stub state if the issue was to finish it).
3. **Schema-init** — the `oscar-household-init` sidecar ran `alembic upgrade head` cleanly; `oscar.db` schema is current.
4. **voice-gatekeeper up** — the Wyoming bridge connects with no `AsrModel.__init__()`-class crash; STT/TTS handoff to Hermes works.
5. **Golden path** — exercise what's wired end-to-end: a voice/chat command flows (oscar-voice → gatekeeper → Hermes → HA-MCP / a skill) and returns a sane result; multimodal ingestion writes a note to `/opt/data/notes`; `hermes-webui` is reachable through NPM.
6. **Observe real behaviour.** Don't claim success from logs alone where you can actually drive the path. If you can't exercise a path (no browser libs, no audio satellite), say so explicitly rather than asserting it works.

**On any failure → route cross-repo** (see "Cross-repo issue routing" in Step 3):
- OSCAR-owned defect → fix here (bite-sized) or file an `mdopp/oscar` issue and work it.
- ServiceBay-platform defect → file an `mdopp/servicebay` issue (symptom + servicebay file/line + the OSCAR repro), mark the related OSCAR item `blocked` `"waiting on mdopp/servicebay#<N>"`, record in `state.upstream_waits[]`, and **wait** — re-check on later invocations and unblock when upstream merges.
- Mixed → split across both repos and note the dependency.

Record the run + outcome + date in `state.last_e2e` and `state.notes[]`. A fully-green E2E with the issue queue empty is the one clean place to **stop** the loop and report.

## Hard exit conditions (stop the loop entirely)

1. CI red on the same PR twice without code changes in between.
2. `state` shows >3 security/privacy-gate drafts accumulated without human review.
3. Working tree dirty at preflight on two consecutive invocations.
4. `/verify` failed against the box twice on the same PR without code changes in between.
5. **Both** the issue queue (after exclusion) **and** the track-(a)/(c) work are empty/exhausted — but prefer track (c)/(d) over exiting (they refill the queue / are the goal), so this is rare.
6. Every remaining open issue is `blocked` on an unmerged `mdopp/servicebay` upstream fix (`state.upstream_waits[]`) — nothing in OSCAR is actionable until ServiceBay ships it. Report the upstream issue links and wait for the next firing.

## Things this skill explicitly does NOT do

- Does not bump versions in `pyproject.toml` or push `v*` tags — releases are the user's call.
- Does not run `gh pr merge --auto` (no branch protection).
- Does not write `--fill`-only PR bodies.
- Does not refactor beyond ticket scope.
- Does not auto-merge any security/privacy-gated issue — those open as draft.
- Does not skip real-box `/verify` on path-mandated PRs (templates/skills/services/migrations/plugin).
- Does not assume CI gates a template-only or skill-only PR — those have no CI; `/verify` is the gate.
- Does not file new issues except in track (c) (codebase eval); otherwise comments on the existing issue.

## Reference

- Repo: `mdopp/oscar`. Labels: `bug`, `enhancement`, `skill`, `template`, `infrastructure`, `phase-0`, `phase-1`, `documentation`, `good first issue`, `help wanted`, `question`.
- Real-box access: `<SERVICEBAY_BOX>` — the **same** box and access paths ServiceBay uses (SSH / HTTP API / MCP; host-key-change, stale-MCP-token, and `Origin`-header gotchas all apply). OSCAR is deployed onto it *through* ServiceBay, so its `mdopp/oscar` registry must be enabled there. `<SERVICEBAY_BOX>` is a placeholder for the box's actual SSH/HTTP/MCP address — supply it from local config (project `CLAUDE.md` or memory), not committed to this public repo.
- Relationship: OSCAR templates/skills are ServiceBay Pod-YAML artifacts; `voice-gatekeeper`/`database` are containers inside the `oscar-household` pod (hostNetwork) that reach ServiceBay's `voice` template via host loopback.
- CI: `.github/workflows/build-images.yml` builds `oscar-gatekeeper` + `oscar-household-init` images (PR = build-only on `voice-gatekeeper/**`+`database/**` paths; push/tag = publish to GHCR).
- Python gates: `voice-gatekeeper` has pytest (`pip install -e '.[test]' && pytest`); `database` is alembic.
