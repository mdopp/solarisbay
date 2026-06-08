---
name: autoloop-issues
description: Orchestrates an autonomous issue-resolution pipeline for mdopp/solbay — Planner → Builder → Verify — coordinated through a shared work queue, spawning each stage as a fresh sub-agent so the loop session stays clean. Verify runs in the BACKGROUND (writes its own result file) so the builder keeps build-ahead-ing the next batch while a prior batch is verified on the real ServiceBay box; only the seal→release critical section serializes. Fast per-issue gates, expensive pipeline (CI + real-box /verify) once per batch. Security/privacy-sensitive issues open as a DRAFT PR and wait for human review (pre-merge gate). Resumable via .claude/state/work-queue.json. Use when the user asks to "burn down the backlog", "work the solbay issues autonomously", or invokes /loop with this skill.
---

# Autoloop orchestrator — mdopp/solbay

You are the **coordinator** of an autonomous issue-resolution pipeline. You do **not** write code, groom issues, or verify the environment yourself — you run a tight dispatch loop that **spawns a fresh sub-agent per stage** and routes work between them through one shared file, `.claude/state/work-queue.json`.

Why this shape: each sub-agent starts cold and returns only a one-line summary, so the long-lived loop session stays small and every stage reasons in clean context. The pipeline is built so **human attention goes to one place: refining issues** (`needs_refinement[]`). Everything downstream — grouping, building, verifying — runs without you.

```
            ┌──────────────── you (orchestrator, this session) ────────────────┐
            │ preflight → read queue → dispatch ONE stage agent → re-read → cadence │
            └──────────────────────────────────────────────────────────────────┘
 PLANNER ──fills──▶ work-queue.json ──┬─▶ BUILDER ──merges, sets verify=owed──┐
 groom/cluster/                       │   fast gates, batch seal,             │
 decompose/refine                     │   push→CI→merge                       ▼
                                      └─▶ BUILDER build-aheads          VERIFY (BACKGROUND) ──gates──▶ release
                                          next batch concurrently       deploy through ServiceBay,
                                          (no main, no release)          /verify on the box, restore
                                                                         writes verify-result.json

  VERIFY runs in the BACKGROUND (Agent run_in_background) and writes its verdict to its OWN file
  (.claude/state/verify-result.json); the orchestrator folds it into verify_state at preflight
  (single writer). While it runs, the builder keeps BUILDING the next batch. Only the
  seal→release critical section serializes; building is concurrent with it.
```

**What Solilos is, and why verification is unusual.** Solilos is *not* a standalone app. It is a bundle of ServiceBay artifacts: `templates/{hermes,hermes-webui,ollama,solbay}/` (Pod-YAML templates), `templates/solbay/skills/*/SKILL.md` (Hermes skills delivered to the node by ServiceBay's asset-transport), `voice-gatekeeper/` (a Python Wyoming↔Hermes bridge — the code-heavy part, package `voice-gatekeeper/src/gatekeeper/`, pytest in `voice-gatekeeper/tests/`, built as image `solilos-gatekeeper`), `database/` (alembic schema-init sidecar, image `schema-init`), `stacks/solbay/stack.yml`, and `plugin.yaml`/`__init__.py` (Hermes Install-from-Git packaging). Solilos runs **on ServiceBay**, on the same box ServiceBay uses (`<SERVICEBAY_BOX>`). So real-environment `/verify` means *deploying the changed Solilos artifact through ServiceBay onto that box and checking the Solilos runtime* — see `stages/verify.md`.

Any project `CLAUDE.md` or user memory overrides this skill on conflict. Read them before the first iteration of a fresh `/loop` run.

## The shared work queue (the only handoff)

`.claude/state/work-queue.json` is the single source of truth between stages. Stage agents read it, do their work, **write results back into it**, and return one line. You re-read it after every spawn. Schema in `work-queue-template.json` (same dir); create from it if absent.

Key fields:
- `queue[]` — **units** the builder consumes, in selection order. A unit is `{id, kind: "cluster"|"issue"|"lint-sweep", issues[], theme, region, scope, acceptance, gate: "normal"|"verify", security: false, status: "planned"|"in_progress"|"built"|"blocked", pr, notes}`. A cluster is the work-unit; its members never appear standalone. `gate` is the verification level; `security: true` routes the unit to the **pre-merge draft gate** (see below).
- `batch` — the persistent integration branch: `{branch, units[], count, sealed}`. **Survives across firings.** Reset to `null` after its release/merge completes.
- `needs_refinement[]` — **the human's worklist.** `{issue, question, comment_url, since}`. The planner parks anything it can't make actionable without a human decision here, with the *specific* question.
- `awaiting_user[]` — external human comment unanswered; never the pipeline's to reply to.
- `review[]` — **the human's pre-merge review list**: `{issue, pr, flag, since}` for `security:true` changes opened as **draft** PRs. Solilos touches biometric speaker-ID, per-resident privacy, and gateway/HA credentials, so security/privacy changes are reviewed **before** they ship (the pre-merge opt-in — see `stages/builder.md`). These are **never auto-merged** by the loop.
- `verify_state` — `{sha, status: "owed"|"verifying"|"red"|"green", detail, since}`. Gates the release/tag. State machine: `owed` (path-mandated change merged, not yet verified on the box) → `verifying` (a background Verify agent is in flight) → `green`|`red`. You set `verifying` when you launch the background agent; the agent writes its verdict to `.claude/state/verify-result.json` (**its own file, not the shared queue** — avoids a write-race with the concurrent builder), and you fold that verdict back into this field at preflight. A `verifying` entry whose `since` is >20 min old with no result file = the agent died → reset to `owed` (it relaunches).
- `blocked[]` — parked work, each `{issue, blocked_by, reason, since}` where `blocked_by` is a **machine-checkable unblock condition** (`"#<N>"` dependency · `"capability:<x>"` · `"decomposition"` · `"epic"` · `"servicebay#<N>"` upstream wait) the planner rechecks every run.
- `upstream_waits[]` — `{issue, servicebay_issue, reason, since}`: a local issue blocked on an unmerged **mdopp/servicebay** platform fix the Solilos loop can't make itself. The planner re-checks whether the upstream issue closed and unblocks.
- `completed[]`, `lint_sweep[]`, `release_warnings[]`, `last_codebase_eval`, `last_e2e`, `notes[]`.

**Label mirror (one-way projection).** The queue file is the source of truth; human-facing states are *mirrored* to GitHub labels so a human sees the same worklist: `blocked[]` → `autoloop:blocked`, `needs_refinement[]` → `autoloop:needs-refinement` (both reconciled by the **planner**), and `verify_state` → `autoloop:verify-pending`/`-failed` on the open batch PR (set here in preflight; Solilos has no release PR — see Step 0.6). Labels are derived from the file every run — never the reverse — so drift is cosmetic and self-heals.

## Batch economy — the prime directive (ENFORCED)

The expensive pipeline — full gates, CI, real-box `/verify` — runs **once per batch (up to 8 closed issues), never once per issue.** All fixes accumulate on ONE long-lived branch `batch/<id>`; it is pushed / PR'd / CI'd / merged / verified **only when it holds 8 closed issues OR the queue of planned units is empty.** Shipping one issue as its own PR while planned units remain is a **failure of this pipeline**.

The builder enforces the per-issue side (fast gates only, commit to the batch branch, no push). You enforce the batch side: **never dispatch a seal step while `batch.count < 8` AND planned units remain.**

**Build-ahead is allowed; seal-ahead is not.** Verify runs in the background (it touches only the box env via ServiceBay and its own result file). The builder may keep **building** the next batch onto a fresh `batch/<id>` branch while a prior batch is being verified — building writes neither `main` nor the box, so it overlaps safely. What must **not** overlap is the singleton critical section: there is one `main`, one box, one `verify_state`, so **a new batch may not be *sealed* while `verify_state.status` is `owed`/`verifying`/`red`** (a prior batch is still in merge/verify). Build up to 8 then *wait* for the verify to clear before sealing.

## Step 0 — Preflight (every firing)

1. **Working tree clean?** `git status --porcelain`. Dirty → exit (another session owns this tree). Don't stash/switch.
2. **On `main`, current?** `git fetch origin && git checkout main && git pull --ff-only`. FF fails → exit + report.
3. **Lock check.** `.claude/state/autoloop.lock` mtime < 10 min ⇒ another firing is running → exit. Else touch it.
4. **Read the work queue.** Create from `work-queue-template.json` if absent. Seed `started`/`last_invocation`.
5. **Fold in any background Verify result.** If `.claude/state/verify-result.json` exists, the background agent finished: copy its `{sha, status, detail, verified_at}` into `verify_state` (you are the single writer of the shared queue's `verify_state`), then **delete the result file**. If `verify_state.status == "verifying"` but no result file exists and `since` is >20 min old, the agent died — reset `verify_state.status` to `"owed"` so it relaunches.
6. **Release gate.** Releases are managed by **release-please**: on each push to `main` it maintains a `chore(main): release X.Y.Z` PR that bumps the version + `CHANGELOG.md` from the conventional commits, and **merging that PR** cuts the `vX.Y.Z` tag + GitHub release (which triggers `build-images.yml` to publish `solilos-gatekeeper` + `schema-init` to GHCR). You **never merge the release PR yourself** and never create/push tags or bump versions in `pyproject.toml` — cutting a release is a human/explicit-ask decision. When release-please's release PR is open you **may** note it in `release_warnings[]` (don't merge). The gate you enforce here is `verify_state`: a merged batch whose path-mandated changes are `owed`/`verifying`/`red` is **not** clear, and you must not seal the next batch until it goes `green`. If a release is warranted after a green verify, log a suggestion in `release_warnings[]`/`notes[]` — don't tag, don't merge the release PR. Mirror `verify_state` onto the open batch PR as a label if one is still open (`owed`/`verifying` → `autoloop:verify-pending`; `red` → `autoloop:verify-failed`; `green`/`null` → remove both).

## Step 1 — Dispatch (the loop body)

**First, a non-blocking side-action (does NOT consume the tick):** if `verify_state.status == "owed"`, launch Verify **in the background** (Step 2, `run_in_background: true`), set `verify_state.status = "verifying"` and `since = now`, and **fall through** to pick a foreground stage below. If `verify_state.status == "verifying"`, an agent is already in flight — don't relaunch; fall through. The background verify clears the release gate on its own time; you don't wait on it here.

Then pick **exactly one** foreground stage this tick, by the first matching rule, and spawn it (Step 2). Then re-read the queue and loop.

1. **Builder — seal** — if a `batch` exists and (`batch.count >= 8` **or** `queue[]` has no `planned` unit) and it isn't merged yet **and `verify_state.status` is clear** (`green`/`null` — *not* `owed`/`verifying`/`red`). Builder runs full gates + CI (where CI applies), merges, sets `verify_state=owed` if any merged file is path-mandated. **Seal-ahead is forbidden:** if `verify_state` is `owed`/`verifying`/`red`, a prior batch is still in merge/verify — do **not** seal; build-ahead instead (rule 2), or idle-wait (Step 3).
2. **Builder — build** — if `queue[]` has a `planned` unit and `batch.count < 8`. Builder implements the next unit onto the batch branch with fast gates only. **This is the build-ahead path** — eligible even while a background Verify runs, because building touches neither `main` nor the box.
3. **Planner** — if there's no actionable unit. Planner refills: groom + cluster open issues, decompose epics, park refinement/awaiting-user/upstream-waits (security issues become `security:true` units that route to the draft gate, not parked), or (queue dry) enqueue lint-sweep units, run a codebase eval, or run **end-to-end validation on the box** + route failures cross-repo to mdopp/servicebay.

Never jump to seal while mid-batch (`count < 8` and planned units remain) — that's the prime-directive violation. Keep building. If the only thing left is to wait on a background Verify (batch built out to 8, nothing to plan), don't dispatch a foreground stage — go to Step 3 and schedule a short wakeup.

## Step 2 — Spawning a stage agent

Use the **Agent** tool, `subagent_type: "general-purpose"` (needs Bash, gh, SSH/MCP env tools, Edit/Write).

**Planner and Builder run foreground (blocking)** — they share `main`, the batch branch, and the shared queue file, so one foreground stage per tick keeps that file single-writer. **Verify runs in the background** (`run_in_background: true`) — it touches only the box (via ServiceBay) and its own result file, so it overlaps with the builder safely.

Foreground (Planner / Builder) prompt — they read & write the shared queue:
```
Read .claude/skills/autoloop-issues/stages/<planner|builder>.md and follow it exactly.
Context for this run: <unit id / batch state to act on>.
The shared queue is .claude/state/work-queue.json — read it, write results back (unit status,
completed/review/needs_refinement/upstream_waits/…, set verify_state=owed at seal), and return ONE
line: what you did + the mutations you made. Do not narrate.
```

Background (Verify) prompt — it does **NOT** touch the shared queue (avoids a write-race with the concurrent builder); it writes its verdict to its own file:
```
Read .claude/skills/autoloop-issues/stages/verify.md and follow it exactly.
Context for this run: verify SHA <verify_state.sha>, path-mandated paths: <verify_state.detail>.
Box: <SERVICEBAY_BOX> (supply the real address from CLAUDE.md / memory).
Do NOT write .claude/state/work-queue.json. Write your verdict to .claude/state/verify-result.json as
{sha, status:"green"|"red"|"owed", detail, verified_at}. The orchestrator folds it into the queue.
Return ONE line: the verdict + any revert PR or upstream issue you opened. Do not narrate.
```

Builder mode (`build` vs `seal`) and the unit `gate`/`security` go in the context line. After a **foreground** agent returns: **re-read `work-queue.json`** (the file is authoritative, not the summary), append the one-liner to your tally, go back to Step 1. The **background** Verify does not block — proceed immediately; its result is folded in at the next preflight (Step 0.5), and the harness re-invokes the loop when it completes.

### Model per stage — match the model to the cost of being wrong

Set `model` on each Agent call. A weak model on real code *costs* time (rework); don't downgrade where being wrong is expensive — do downgrade mechanical work.

| Stage / unit | Model |
|---|---|
| Builder — real code (`cluster`/`issue`) | `opus` |
| Builder — `lint-sweep` unit | `haiku` |
| Planner | `sonnet` |
| Verify | `sonnet` |

The orchestrator itself is pure dispatch and runs at the session model — a light model is fine for it.

## Step 3 — Cadence (`/loop` dynamic mode)

**Never sleep while there is eligible work** — go straight to the next dispatch. A **background Verify in flight is not a reason to sleep** if there's still a unit to build: leave it running and keep building the next batch. `ScheduleWakeup` only when:
- **Mid-pipeline waiting on an external gate** (CI on an image-path PR, or a ServiceBay install/deploy on the box) → `delaySeconds ≤ 480`, prefer ~60s if imminent.
- **Build-ahead exhausted, only a background Verify outstanding** (batch built out to 8, nothing left to plan, can't seal until verify clears) → `≤ 480`. The harness also re-invokes you when the background agent completes, so this is a fallback heartbeat.
- **Queue empty and planner found nothing** (and an e2e/eval ran recently) → idle heartbeat `≤ 480`.

Pass the same `/loop /autoloop-issues` input back. Don't nap between dispatches when work remains.

## Comment hygiene

Every comment any stage posts is attributable as agent-authored (an AI marker if posted as a human account), and stays short and sharp. **No stage ever replies to an external human commenter** — those tickets are parked on `awaiting_user[]` for a human-confirmed reply.

## End-of-firing summary

```
Autoloop (solbay) firing complete.
  Built this firing: <unit ids> → batch/<id> (count N/8)
  Merged batches:    PR #<n> (closes #a #b …)
  Verify:            green @ <sha> on the box | verifying (background) | owed | red (<detail>)
  Review pre-merge:  #<issue> (draft #<pr>) — security/privacy, NOT merged   ← review these
  Needs refinement:  #<issue> — "<question>"   ← your worklist
  Upstream waits:    #<issue> → servicebay#<N>
  Awaiting user:     #<issue> (external comment)
Next: <building #x | sealing batch | verifying | planner refill | e2e | idle heartbeat>.
```

The **Needs refinement** line is the point of the pipeline.

## Hard exit conditions (stop; do not reschedule)

1. A stage reports CI red twice on the same SHA with no change between.
2. `review[]` shows >3 security/privacy draft PRs accumulated without human review.
3. Working tree dirty at preflight on two consecutive firings.
4. A `/verify` failed twice on the same SHA with no change between, or the box was left in a staging state the Verify stage couldn't restore (env must not be left in the test state).
5. Planner's issue queue and lint set both empty AND a codebase eval ran within the last ~5 firings AND an e2e ran since the last merge.
6. Every open issue is blocked on an unmerged **mdopp/servicebay** upstream fix (`upstream_waits[]`) — nothing in Solilos is actionable until ServiceBay ships it. Report the upstream links and wait.

## Things this orchestrator does NOT do
- Write code / groom / `/verify` itself — only dispatches stage agents.
- Bump versions in `pyproject.toml`, create/push `v*` tags, or merge release-please's release PR — releases are the user's call (release-please maintains the release PR; merging it is the human gate).
- `gh pr merge --auto` (no branch protection → silent no-op); reply to external commenters.
- Dispatch a seal step while mid-batch (prime directive).
- **Seal** a new batch while a prior batch's `verify_state` is `owed`/`verifying`/`red` (seal-ahead forbidden — one batch in the merge/verify critical section at a time). It *may* build-ahead.
- Block the loop on Verify — that runs in the background; the builder keeps building while it does.
- Ship/merge a path-mandated change without a green real-box `/verify` (gate via `verify_state`).
- Auto-merge a `security:true` change — those open as draft and wait for human review.

## Reference
- Stages: `stages/planner.md`, `stages/builder.md`, `stages/verify.md` (this dir; Verify runs in the background and writes `.claude/state/verify-result.json`). Queue schema: `work-queue-template.json`. How to run: `USAGE.md`.
- Repo: `mdopp/solbay`. Upstream platform: `mdopp/servicebay` (cross-repo routing — see `stages/{planner,verify}.md`).
- Real-box access: `<SERVICEBAY_BOX>` — the **same** box and access paths ServiceBay uses (SSH / HTTP API / MCP; host-key-change, stale-MCP-token, and `Origin`-header gotchas all apply). The `mdopp/solbay` registry must be enabled in ServiceBay on that box so changed templates resolve. `<SERVICEBAY_BOX>` is a placeholder — supply the real SSH/HTTP/MCP address from local config (project `CLAUDE.md` or memory), **never** commit it to this public repo.
- CI: `.github/workflows/ci.yml` (ruff + pytest + semgrep + pip-audit + diff-coverage, path-filtered to Python/config paths) and `build-images.yml` (builds the two images on PR for image paths, publishes on `main`/tags). **Template-only / skill-only / docs-only PRs trigger no CI** — for those the gate is local validation + real-box `/verify`.
- Worked example: `mdopp/servicebay` (`.claude/skills/autoloop-issues/`) — its Verify stage is `box-verify.md` (a `:dev`/`:latest` channel flip); Solilos's Verify deploys the changed artifact through ServiceBay instead.
